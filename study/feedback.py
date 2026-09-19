"""社区反馈、伦理溯源与定向通知。

隐私流向的核心约束：

- 社区报告只到“社区 + 粗场所类型”粒度，样本量低于阈值的格子被抑制，
  社区无法从反馈反推任何家庭；
- 伦理人员看到的是聚合结论，并可沿“冻结 → 输入行 → 同意/校准质量 →
  分析编号 → 家庭”的链条逐步追查，每一步都写审计；
- 发现风险时由伦理发起定向通知，通知载荷只有联系信息与健康建议，
  不包含暴露数值或暴露排名，且只发给受影响家庭。
"""

import threading

from study.catalog import AGE_BAND_LABELS
from study.errors import NotFound, PermissionDenied, ValidationError
from study.util import new_id

# 社区反馈中每个格子最少需要的独立家庭/独立人天数，低于则抑制。
MIN_CELL_HOUSEHOLDS = 3
MIN_CELL_DAYS = 5


class FeedbackService:
    def __init__(self, catalog, vault, sensors, exposure, estimates, audit):
        self._catalog = catalog
        self._vault = vault
        self._sensors = sensors
        self._exposure = exposure
        self._estimates = estimates
        self._audit = audit

    def community_report(self, actor, community_id, freeze_id):
        """生成面向社区的反馈：粗粒度、小样本抑制、无家庭标识。"""
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"community", "ethics", "admin", "analyst"}:
            raise PermissionDenied(f"角色 {role} 无权查看社区反馈")
        community = self._catalog.get_community(community_id)
        freeze = self._estimates.get_freeze("ethics", freeze_id)

        region = freeze["regions_by_code"][community["region_code"]]
        change = region["change"]

        cells = []
        for phase in ("pre", "post"):
            stats = region["phases"][phase]
            # 逐年龄段格子：独立家庭数与有效人天不足时抑制。
            band_cells = []
            for band, label in AGE_BAND_LABELS.items():
                stratum = stats.get("strata", {}).get(band)
                suppression = self._cell_suppression(
                    community_id, phase, band)
                if stratum is None or suppression is not None:
                    band_cells.append({
                        "age_band": band,
                        "label": label,
                        "suppressed": True,
                        "reason": suppression or "no_measurements",
                    })
                else:
                    band_cells.append({
                        "age_band": band,
                        "label": label,
                        "suppressed": False,
                        "mean_pm25": stratum["mean"],
                        "measured_days": stratum["n_days"],
                    })
            cells.append({"phase": phase, "age_bands": band_cells})

        # 公共场所反馈：只给类型聚合，不给可定位场所。
        venue_rows = self._venue_type_summary(community_id)

        report = {
            "community_id": community_id,
            "region_code": community["region_code"],
            "freeze_id": freeze_id,
            "message": self._summary_message(change),
            "change": None if change["status"] != "ok" else {
                "std_change": change["std_change"],
                "ci_lower": change["ci_lower"],
                "ci_upper": change["ci_upper"],
                "composition_driven": change["composition_driven"],
            },
            "household_cells": cells,
            "venue_type_summary": venue_rows,
            "suppression_threshold": {
                "min_households": MIN_CELL_HOUSEHOLDS,
                "min_days": MIN_CELL_DAYS,
            },
        }
        self._audit.append(actor, "community_report_viewed",
                           community_id=community_id, freeze_id=freeze_id)
        return report

    def _cell_suppression(self, community_id, phase, band):
        """返回抑制原因；不抑制返回 None。"""
        rows = [r for r in self._exposure.contributions("ethics")
                if r["community_id"] == community_id
                and r["policy_phase"] == phase
                and r["age_band"] == band]
        measured = [r for r in rows if r["measured"]]
        if len(measured) < MIN_CELL_DAYS:
            return "insufficient_days"
        households = {self._vault.household_of_analysis(r["analysis_id"])[0]
                      for r in measured}
        if len(households) < MIN_CELL_HOUSEHOLDS:
            return "small_household_count"
        return None

    def _venue_type_summary(self, community_id):
        grouped = {}
        for day in self._exposure.venue_days("ethics",
                                             venue_type=None):
            if day["community_id"] != community_id:
                continue
            cell = grouped.setdefault(day["venue_type"], {
                "venue_type": day["venue_type"], "values": [],
                "missing_windows": 0})
            cell["missing_windows"] += day["missing_windows"]
            if day["pm25_mean"] is not None:
                cell["values"].append(day["pm25_mean"])
        rows = []
        for venue_type, cell in grouped.items():
            if len(cell["values"]) < MIN_CELL_DAYS:
                rows.append({"venue_type": venue_type, "suppressed": True,
                             "reason": "insufficient_days"})
                continue
            rows.append({
                "venue_type": venue_type,
                "suppressed": False,
                "mean_pm25": round(sum(cell["values"]) / len(cell["values"]), 3),
                "venue_days": len(cell["values"]),
                "missing_windows": cell["missing_windows"],
            })
        return sorted(rows, key=lambda r: r["venue_type"])

    @staticmethod
    def _summary_message(change):
        if change["status"] != "ok":
            return "数据尚不足以判断本社区政策前后变化，继续监测中。"
        direction = "下降" if change["std_change"] < 0 else "上升"
        driver = "（主要可能来自人口构成变化，需谨慎解读）" \
            if change["composition_driven"] else ""
        return (f"按统一年龄构成调整后，社区二手烟指标{direction}"
                f"{abs(change['std_change'])} 个单位{driver}。")


class EthicsService:
    def __init__(self, catalog, vault, sensors, exposure, estimates, audit):
        self._catalog = catalog
        self._vault = vault
        self._sensors = sensors
        self._exposure = exposure
        self._estimates = estimates
        self._audit = audit

    def aggregate_view(self, actor, freeze_id):
        """伦理总览：聚合结论 + 每地区同意覆盖/数据质量，无个人暴露值。"""
        self._require(actor)
        freeze = self._estimates.get_freeze("ethics", freeze_id)
        regions = []
        for region in freeze["regions"]:
            code = region["region_code"]
            quality = freeze["quality"][code]
            received = quality["valid_readings"] + quality["invalid_readings"]
            coverage = None
            expected = received + quality["missing_windows"]
            if expected:
                coverage = round(received / expected, 3)
            pre = region["phases"]["pre"]
            post = region["phases"]["post"]
            regions.append({
                "region_code": code,
                "estimate_status": {
                    "pre": pre["status"], "post": post["status"]},
                "change": region["change"],
                "consented_person_days": {
                    "pre": pre["person_days_total"],
                    "post": post["person_days_total"],
                },
                "measured_person_days": {
                    "pre": pre["person_days_measured"],
                    "post": post["person_days_measured"],
                },
                "sensor_coverage": coverage,
                "invalid_readings": quality["invalid_readings"],
                "missing_windows": quality["missing_windows"],
            })
        self._audit.append(actor, "ethics_aggregate_viewed",
                           freeze_id=freeze_id)
        return {"freeze_id": freeze_id,
                "window": freeze["window"],
                "region_total": freeze["region_total"],
                "regions": regions}

    def trace_region(self, actor, freeze_id, region_code):
        """从聚合地区下钻：质量问题位置与涉及的分析编号（无身份明文）。"""
        self._require(actor)
        freeze = self._estimates.get_freeze("ethics", freeze_id)
        region = freeze["regions_by_code"].get(region_code)
        if region is None:
            raise NotFound(f"地区不在冻结范围：{region_code}")

        # 失效读数与缺测定位到具体设备/家庭部署（家庭只显示编号）。
        quality_issues = []
        for deployment in self._sensors.deployments():
            location = deployment["location_ref"]
            if deployment["location_kind"] == "household":
                household = self._vault.get_household(location)
                community = self._catalog.get_community(
                    household["community_id"])
                if community["region_code"] != region_code:
                    continue
            else:
                venue = self._catalog.get_venue(location)
                community = self._catalog.get_community(venue["community_id"])
                if community["region_code"] != region_code:
                    continue
            counts = self._sensors.quality_counts(location)
            if counts["invalid_readings"] or counts["missing_windows"]:
                quality_issues.append({
                    "serial": deployment["serial"],
                    "location_kind": deployment["location_kind"],
                    "location_ref": location,
                    **counts,
                })

        # 涉及的分析编号与其同意/退出状态（供下一步追查，不含姓名）。
        affected = {}
        for row in self._exposure.contributions("ethics",
                                                region_code=region_code):
            entry = affected.setdefault(row["analysis_id"], {
                "analysis_id": row["analysis_id"],
                "community_id": row["community_id"],
                "person_days": 0, "measured_days": 0})
            entry["person_days"] += 1
            entry["measured_days"] += 1 if row["measured"] else 0

        result = {
            "freeze_id": freeze_id,
            "region_code": region_code,
            "estimate": region,
            "quality_issues": quality_issues,
            "affected_analysis_ids": sorted(affected.values(),
                                            key=lambda x: x["analysis_id"]),
        }
        self._audit.append(actor, "ethics_region_traced",
                           freeze_id=freeze_id, region_code=region_code,
                           analysis_count=len(affected))
        return result

    def trace_analysis(self, actor, analysis_id):
        """追查单个分析编号：同意区间、居住片段、退出与人天行计数。"""
        self._require(actor)
        # 解析身份这一动作单独审计，但返回体不含姓名/联系方式。
        member_id = self._vault.resolve_analysis_id(actor, analysis_id)
        household_id, community_id = self._vault.household_of_analysis(
            analysis_id)
        contributions = [r for r in
                         self._exposure.contributions("ethics")
                         if r["analysis_id"] == analysis_id]
        result = {
            "analysis_id": analysis_id,
            "community_id": community_id,
            "household_id": household_id,
            "consent_intervals": self._vault.consent_intervals(household_id),
            "residency": self._vault.residency_episodes(analysis_id),
            "withdrawal": self._vault.withdrawal(analysis_id),
            "person_days": len(contributions),
            "measured_days": sum(1 for r in contributions if r["measured"]),
            # 不返回任何暴露数值，避免伦理侧形成个人暴露画像。
        }
        self._audit.append(actor, "ethics_analysis_traced",
                           analysis_id=analysis_id, member_id=member_id,
                           person_days=len(contributions))
        return result

    def open_identity(self, actor, analysis_id, reason):
        """伦理因明确风险需要身份时，记录理由后解析联系方式所属家庭。"""
        self._require(actor)
        if not reason:
            raise ValidationError("开启身份必须填写风险理由")
        member_id = self._vault.resolve_analysis_id(actor, analysis_id)
        household_id, _ = self._vault.household_of_analysis(analysis_id)
        contact = self._vault.get_household_identity(actor, household_id)
        self._audit.append(actor, "identity_opened_for_risk",
                           analysis_id=analysis_id, household_id=household_id,
                           reason=reason)
        return {"analysis_id": analysis_id,
                "household_id": household_id,
                "contact_name": contact["contact_name"],
                "contact_phone": contact["contact_phone"]}

    @staticmethod
    def _require(actor):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"ethics", "admin"}:
            raise PermissionDenied(f"角色 {role} 无权使用伦理溯源")


class NotificationService:
    def __init__(self, catalog, vault, audit):
        self._catalog = catalog
        self._vault = vault
        self._audit = audit
        self._notifications = {}
        self._lock = threading.Lock()

    def issue(self, actor, analysis_ids, reason, advice, channel="field_visit"):
        """伦理发起定向通知。

        - 载荷只有家庭联系信息与健康建议，绝不含暴露数值；
        - 同一家庭在一次通知中只出现一次（成员去重到家庭）；
        - 只有伦理/管理员可发起，现场角色只负责后续派发。
        """
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"ethics", "admin"}:
            raise PermissionDenied(f"角色 {role} 无权发起定向通知")
        if not analysis_ids:
            raise ValidationError("至少指定一个受影响分析编号")
        if not advice:
            raise ValidationError("通知必须包含健康建议内容")

        targets = []
        seen_households = set()
        for analysis_id in analysis_ids:
            household_id, community_id = \
                self._vault.household_of_analysis(analysis_id)
            if household_id in seen_households:
                continue
            seen_households.add(household_id)
            contact = self._vault.household_contact_for_notification(
                "ethics", household_id)
            targets.append({
                "household_id": household_id,
                "community_id": community_id,
                "contact_name": contact["contact_name"],
                "contact_phone": contact["contact_phone"],
            })

        with self._lock:
            notification_id = new_id("N", self._notifications)
            record = {
                "notification_id": notification_id,
                "reason": reason,
                "advice": advice,
                "channel": channel,
                "status": "issued",
                # 审计明细里只留分析编号；通知主体不含暴露数据。
                "analysis_ids": list(analysis_ids),
                "targets": targets,
                "deliveries": {t["household_id"]: "pending" for t in targets},
            }
            self._notifications[notification_id] = record
        self._audit.append(actor, "notification_issued",
                           notification_id=notification_id,
                           household_count=len(targets),
                           reason=reason)
        return self._view(record)

    def mark_delivered(self, actor, notification_id, household_id,
                       outcome="delivered"):
        """现场回填派发结果。"""
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"field", "ethics", "admin"}:
            raise PermissionDenied(f"角色 {role} 无权回填派发结果")
        with self._lock:
            record = self._notifications.get(notification_id)
            if record is None:
                raise NotFound(f"通知不存在：{notification_id}")
            if household_id not in record["deliveries"]:
                raise ValidationError("该家庭不在此通知的目标名单中")
            record["deliveries"][household_id] = outcome
        self._audit.append(actor, "notification_delivered",
                           notification_id=notification_id,
                           household_id=household_id, outcome=outcome)
        return self._view(record)

    def get(self, actor, notification_id):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"ethics", "admin", "field"}:
            raise PermissionDenied(f"角色 {role} 无权读取通知")
        record = self._notifications.get(notification_id)
        if record is None:
            raise NotFound(f"通知不存在：{notification_id}")
        return self._view(record)

    def list_for_field(self, actor, community_id=None):
        """现场派发清单：联系方式可见，但不含分析编号与暴露信息。"""
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"field", "ethics", "admin"}:
            raise PermissionDenied(f"角色 {role} 无权读取派发清单")
        records = sorted(self._notifications.values(),
                         key=lambda r: r["notification_id"])
        result = []
        for record in records:
            targets = [t for t in record["targets"]
                       if community_id is None
                       or t["community_id"] == community_id]
            if not targets:
                continue
            result.append({
                "notification_id": record["notification_id"],
                "advice": record["advice"],
                "channel": record["channel"],
                "targets": targets,
                "deliveries": {t["household_id"]:
                               record["deliveries"][t["household_id"]]
                               for t in targets},
            })
        return result

    @staticmethod
    def _view(record):
        return {
            "notification_id": record["notification_id"],
            "reason": record["reason"],
            "advice": record["advice"],
            "channel": record["channel"],
            "status": record["status"],
            "target_count": len(record["targets"]),
            "targets": [dict(t) for t in record["targets"]],
            "deliveries": dict(record["deliveries"]),
        }
