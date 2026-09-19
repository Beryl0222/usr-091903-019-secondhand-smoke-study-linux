"""暴露人时、人天贡献物化与症状随访。

人时（精确到人天，每天 24 小时）在物化时按真实日期施加四道截断：

1. 家庭同意区间：只有同意有效的日期计人时；
2. 居住片段：搬家当日起计入新家庭/新社区；
3. 成员退出：退出日起不再计人时；
4. 政策生效日：同一社区的人天标记为 pre/post，分期估算。

校准失效或缺失的传感器读数不产生暴露均值，但人天行仍保留并标记
``measured=False``，使“真实下降”与“传感器缺测/人口变化”可区分：
估算时分别报告粗均值、标准化均值与数据质量计数。
"""

import threading
from collections import defaultdict
from datetime import date, timedelta

from study.errors import ValidationError
from study.util import intersect, iso, member_age_band, new_id, parse_date, parse_ts

HOURS_PER_DAY = 24.0


def _daterange(start, end):
    """半开日期区间按天展开。"""
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(days=1)


class ExposureStore:
    def __init__(self, catalog, vault, sensors, audit):
        self._catalog = catalog
        self._vault = vault
        self._sensors = sensors
        self._audit = audit
        self._lock = threading.Lock()
        # 家庭人天（只含分析编号与社区，不含家庭身份字段）。
        self._contributions = {}
        self._contrib_seq = 0
        self._contrib_by_id = {}  # 含被取代的历史版本，供旧冻结溯源
        # 公共场所环境日值（不关联任何个人）。
        self._venue_days = {}
        self._envday_seq = 0
        self._envday_by_id = {}
        self._symptoms = {}

    # ---- 人时物化 ------------------------------------------------------
    def materialize(self, actor, window_start, window_end):
        """把窗口内的人天贡献物化为只增快照行。

        已存在的同一 (analysis_id, 日期) 行不会被覆盖，保证冻结估算
        引用的贡献不可被悄悄改写；重复执行只补齐新增日期（幂等）。
        """
        self._require(actor, {"analyst", "admin"})
        start = parse_date(window_start)
        end = parse_date(window_end)
        if end <= start:
            raise ValidationError("物化窗口结束日必须晚于开始日")

        household_pm = self._daily_means("household")
        venue_pm = self._daily_means("venue")
        venue_quality = self._daily_quality("venue")

        created_contribs = 0
        with self._lock:
            for analysis_id in self._vault.analysis_member_ids():
                birth = self._vault.birth_date_for_analysis(analysis_id)
                withdrawal = self._vault.withdrawal(analysis_id)
                cap = parse_date(withdrawal["date"]) if withdrawal else None
                for episode in self._vault.residency_episodes_raw(analysis_id):
                    community_id = episode["community_id"]
                    household_id = episode["household_id"]
                    consent = [
                        (parse_date(i["start"]), parse_date(i["end"]))
                        for i in self._vault.consent_intervals(household_id)
                    ]
                    for consent_start, consent_end in consent:
                        clip = intersect(
                            episode["start"], episode["end"],
                            consent_start, consent_end,
                        )
                        if clip is None:
                            continue
                        clip = intersect(clip[0], clip[1], start, end)
                        if clip is None:
                            continue
                        seg_start, seg_end = clip
                        if cap is not None:
                            clipped = intersect(seg_start, seg_end,
                                                start, cap)
                            if clipped is None:
                                continue
                            seg_start, seg_end = clipped
                        for day in _daterange(seg_start, seg_end):
                            key = (analysis_id, iso(day))
                            stats = household_pm.get((household_id, iso(day)))
                            pm = stats["mean"] if stats else None
                            existing = self._contributions.get(key)
                            if existing is not None:
                                # 离线补传：旧行为缺测、现在有有效读数时，
                                # 以“取代”方式生成新版本；旧行保留给旧
                                # 冻结复核，新运行只读取取代后的行。
                                if (existing.get("superseded_by") is None
                                        and not existing["measured"]
                                        and pm is not None):
                                    self._contrib_seq += 1
                                    cid = f"C-{self._contrib_seq:06d}"
                                    new_row = dict(existing)
                                    new_row.update({
                                        "contribution_id": cid,
                                        "measured": True,
                                        "pm25_mean": round(pm, 3),
                                        "supersedes": existing[
                                            "contribution_id"],
                                    })
                                    existing.pop("superseded_by", None)
                                    existing["superseded_by"] = cid
                                    self._contrib_by_id[
                                        existing["contribution_id"]] = dict(
                                        existing)
                                    self._contrib_by_id[cid] = dict(new_row)
                                    self._contributions[key] = new_row
                                    created_contribs += 1
                                continue
                            row = {
                                "contribution_id": None,
                                "analysis_id": analysis_id,
                                "date": iso(day),
                                "community_id": community_id,
                                "region_code": self._catalog.get_community(
                                    community_id)["region_code"],
                                "age_band": member_age_band(birth, day),
                                "policy_phase": self._catalog.policy_phase(
                                    community_id, day),
                                "location_kind": "home",
                                "hours": HOURS_PER_DAY,
                                "measured": pm is not None,
                                "pm25_mean": None if pm is None else round(pm, 3),
                            }
                            self._contrib_seq += 1
                            row["contribution_id"] = (
                                f"C-{self._contrib_seq:06d}")
                            self._contributions[key] = row
                            self._contrib_by_id[row["contribution_id"]] = row
                            created_contribs += 1

            created_venue_days = 0
            # 为窗口内每个“部署日”生成环境日行：完全缺测的日子
            # pm25_mean 为 None，但缺测窗口计入质量汇总。
            for (location_ref, day), quality in sorted(venue_quality.items()):
                if not (start <= parse_date(day) < end):
                    continue
                venue = self._catalog.get_venue(location_ref)
                community = self._catalog.get_community(venue["community_id"])
                key = (location_ref, day)
                stats = venue_pm.get(key)
                pm = stats["mean"] if stats else None
                existing = self._venue_days.get(key)
                payload = {
                    "venue_id": location_ref,
                    "venue_type": venue["venue_type"],
                    "community_id": venue["community_id"],
                    "region_code": community["region_code"],
                    "date": day,
                    "policy_phase": self._catalog.policy_phase(
                        venue["community_id"], parse_date(day)),
                    "pm25_mean": None if pm is None else round(pm, 3),
                    "valid_readings": stats["n"] if stats else 0,
                    "invalid_readings": quality["invalid"],
                    "missing_windows": quality["missing"],
                }
                if existing is not None:
                    # 与家庭人天行相同的补传取代规则。
                    changed = (existing["pm25_mean"] != payload["pm25_mean"]
                               or existing["missing_windows"]
                               != payload["missing_windows"]
                               or existing["invalid_readings"]
                               != payload["invalid_readings"])
                    if changed and existing.get("superseded_by") is None:
                        self._envday_seq += 1
                        eid = f"E-{self._envday_seq:06d}"
                        new_row = dict(payload)
                        new_row["envday_id"] = eid
                        new_row["supersedes"] = existing["envday_id"]
                        existing.pop("superseded_by", None)
                        existing["superseded_by"] = eid
                        self._envday_by_id[existing["envday_id"]] = dict(
                            existing)
                        self._envday_by_id[eid] = dict(new_row)
                        self._venue_days[key] = new_row
                        created_venue_days += 1
                    continue
                self._envday_seq += 1
                payload["envday_id"] = f"E-{self._envday_seq:06d}"
                self._venue_days[key] = payload
                self._envday_by_id[payload["envday_id"]] = payload
                created_venue_days += 1

        self._audit.append(
            actor, "contributions_materialized",
            window_start=iso(start), window_end=iso(end),
            person_days=created_contribs, venue_days=created_venue_days)
        return {"window_start": iso(start), "window_end": iso(end),
                "person_days": created_contribs,
                "venue_days": created_venue_days}

    def _daily_means(self, location_kind):
        """按场所/家庭聚合校准有效读数的日均值。失效读数自动排除。"""
        bucket = defaultdict(list)
        for reading in self._sensors.query_readings(valid_only=True):
            if reading["location_kind"] != location_kind:
                continue
            day = parse_ts(reading["sample_start"]).date().isoformat()
            bucket[(reading["location_ref"], day)].append(reading["pm25"])
        return {key: {"mean": sum(values) / len(values), "n": len(values)}
                for key, values in bucket.items()}

    def _daily_quality(self, location_kind):
        """按 (场所, 日) 统计有效/失效读数与缺测窗口。

        应有窗口来自部署区间与采样间隔（开放部署截至今天），
        缺测 = 应有窗口 - 已收到窗口（无论校准是否有效）。
        """
        observed = defaultdict(lambda: {"valid": 0, "invalid": 0})
        for reading in self._sensors.query_readings():
            if reading["location_kind"] != location_kind:
                continue
            day = parse_ts(reading["sample_start"]).date()
            key = (reading["location_ref"], iso(day))
            observed[key][reading["calibration_status"]] += 1

        expected = defaultdict(int)
        for deployment in self._sensors.deployments():
            if deployment["location_kind"] != location_kind:
                continue
            start = parse_date(deployment["start_date"])
            end = parse_date(deployment["end_date"])
            if end is None:
                end = date.today()
            slots_per_day = (24 * 60) // deployment["interval_minutes"]
            for day in _daterange(start, end):
                expected[(deployment["location_ref"], iso(day))] += slots_per_day

        quality = {}
        for key, slots in expected.items():
            counts = observed.get(key, {"valid": 0, "invalid": 0})
            quality[key] = {
                "valid": counts["valid"],
                "invalid": counts["invalid"],
                "missing": max(slots - counts["valid"] - counts["invalid"], 0),
            }
        return quality

    # ---- 查询 ----------------------------------------------------------
    def contributions(self, actor, region_code=None, community_id=None,
                      measured_only=False):
        self._require(actor, {"analyst", "ethics", "admin"})
        rows = list(self._contributions.values())
        if region_code is not None:
            rows = [r for r in rows if r["region_code"] == region_code]
        if community_id is not None:
            rows = [r for r in rows if r["community_id"] == community_id]
        if measured_only:
            rows = [r for r in rows if r["measured"]]
        return [dict(r) for r in sorted(rows,
                                        key=lambda r: (r["date"],
                                                       r["analysis_id"]))]

    def venue_days(self, actor, region_code=None, venue_type=None):
        self._require(actor, {"analyst", "ethics", "admin"})
        rows = list(self._venue_days.values())
        if region_code is not None:
            rows = [r for r in rows if r["region_code"] == region_code]
        if venue_type is not None:
            rows = [r for r in rows if r["venue_type"] == venue_type]
        return [dict(r) for r in sorted(rows, key=lambda r: r["date"])]

    # ---- 症状随访 ------------------------------------------------------
    def record_symptoms(self, actor, analysis_id, symptom_date, symptoms,
                        severity="mild", report_date=None, notes=None):
        """登记症状随访。编号侧匿名；落在有效人天窗口外会被标记但不删除。"""
        self._require(actor, {"field", "admin"})
        if not symptoms:
            raise ValidationError("至少记录一种症状")
        symptom_day = parse_date(symptom_date)
        report_day = parse_date(report_date) if report_date else date.today()
        # 校验编号存在。
        self._vault.residency_episodes(analysis_id)
        withdrawal = self._vault.withdrawal(analysis_id)
        in_window = (
            (withdrawal is None or symptom_day < parse_date(withdrawal["date"]))
            and self._has_consented_presence(analysis_id, symptom_day)
        )
        with self._lock:
            fid = new_id("F", self._symptoms)
            record = {
                "followup_id": fid,
                "analysis_id": analysis_id,
                "symptom_date": iso(symptom_day),
                "report_date": iso(report_day),
                "symptoms": list(symptoms),
                "severity": severity,
                "in_consent_window": in_window,
                "notes": notes,
            }
            self._symptoms[fid] = record
        self._audit.append(actor, "symptoms_recorded",
                           followup_id=fid, analysis_id=analysis_id,
                           symptom_date=iso(symptom_day),
                           in_consent_window=in_window)
        return dict(record)

    def _has_consented_presence(self, analysis_id, day):
        for episode in self._vault.residency_episodes_raw(analysis_id):
            if not (episode["start"] <= day < (episode["end"] or date.max)):
                continue
            for interval in self._vault.consent_intervals(episode["household_id"]):
                start = parse_date(interval["start"])
                end = parse_date(interval["end"])
                if start <= day < (end or date.max):
                    return True
        return False

    def symptom_followups(self, actor, analysis_id=None, in_window_only=False):
        self._require(actor, {"analyst", "ethics", "admin", "field"})
        rows = list(self._symptoms.values())
        if analysis_id is not None:
            rows = [r for r in rows if r["analysis_id"] == analysis_id]
        if in_window_only:
            rows = [r for r in rows if r["in_consent_window"]]
        return [dict(r) for r in sorted(rows, key=lambda r: r["symptom_date"])]

    @staticmethod
    def _require(actor, allowed):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in allowed:
            from study.errors import PermissionDenied
            raise PermissionDenied(f"角色 {role} 无权执行该操作",
                                   allowed=sorted(allowed))
