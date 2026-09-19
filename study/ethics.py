"""伦理追溯与定向通知支持。

伦理人员可从"聚合结论"（冻结版本 + 地区）向下追查两层：
- 数据质量：进入/被排除读数的数量与原因（校准失效、无校准等）；
- 同意状态：每个人时段冻结时的同意版本，以及同意当前是否仍有效。

追溯结果只含分析编号，不含真实身份；需要联系家庭时，由伦理人员
圈定分析编号发起定向通知，身份库侧才把编号解析为家庭联系方式，
通知话术使用固定模板，不含任何暴露数值。
"""

from .errors import NotFoundError


class EthicsService:
    def __init__(self, analysis_conn, identity_service):
        self.conn = analysis_conn
        self.ids = identity_service

    def _require_version_region(self, version_id, region_code):
        v = self.conn.execute(
            "SELECT 1 FROM estimate_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if v is None:
            raise NotFoundError(f"冻结版本不存在: {version_id}")
        r = self.conn.execute(
            "SELECT * FROM estimate_region_results WHERE version_id=? AND region_code=?",
            (version_id, region_code),
        ).fetchone()
        if r is None:
            raise NotFoundError(f"版本中无该地区结果: {region_code}")
        return r

    def region_trace(self, version_id, region_code):
        result = self._require_version_region(version_id, region_code)

        reading_rows = self.conn.execute(
            "SELECT * FROM estimate_lineage_readings WHERE version_id=? AND region_code=?",
            (version_id, region_code),
        ).fetchall()
        quality = {"included": 0, "excluded": {}, "by_device": {}}
        for r in reading_rows:
            serial = r["device_serial"]
            quality["by_device"].setdefault(serial, {"included": 0, "excluded": 0})
            if r["included"]:
                quality["included"] += 1
                quality["by_device"][serial]["included"] += 1
            else:
                reason = r["reason"] or "unknown"
                quality["excluded"][reason] = quality["excluded"].get(reason, 0) + 1
                quality["by_device"][serial]["excluded"] += 1

        person_rows = self.conn.execute(
            "SELECT * FROM estimate_lineage_persons WHERE version_id=? AND region_code=?",
            (version_id, region_code),
        ).fetchall()
        # 同一成员可能因搬家/政策切成多段，按分析编号聚合为一个人，
        # 段明细保存在 segments 中；同意与症状在成员层面判定。
        persons_by_id = {}
        for p in person_rows:
            link = self.ids.resolve_analysis_id(p["analysis_id"])
            current = self.ids.active_consent(link["household_id"], _today())
            person = persons_by_id.setdefault(p["analysis_id"], {
                "analysis_id": p["analysis_id"],
                "age_band": p["age_band"],
                "role": p["role"],
                "person_days": 0,
                "consent_versions_at_freeze": [],
                "consent_active_at_freeze": True,
                "consent_currently_active": current is not None,
                "current_consent_version": current["version"] if current else None,
                "symptom_alert": False,
                "intervention_ids": [],
                "withdrawn": bool(link["withdrawn_on"]),
                "segments": [],
            })
            person["person_days"] += p["person_days"]
            # 症状预警取自冻结血缘（当时窗口内的随访结果），不随后续随访改变；
            # 同意是否仍然有效则实时核查（见 consent_currently_active）。
            person["symptom_alert"] = person["symptom_alert"] or bool(p["symptom_alert"])
            person["consent_active_at_freeze"] = (
                person["consent_active_at_freeze"] and bool(p["consent_active"]))
            cv = f"{p['consent_id']}@v{p['consent_version']}"
            if cv not in person["consent_versions_at_freeze"]:
                person["consent_versions_at_freeze"].append(cv)
            if p["intervention_id"] and p["intervention_id"] not in person["intervention_ids"]:
                person["intervention_ids"].append(p["intervention_id"])
            person["segments"].append({
                "person_days": p["person_days"],
                "consent_id": p["consent_id"],
                "consent_version": p["consent_version"],
                "consent_active": bool(p["consent_active"]),
                "symptom_alert": bool(p["symptom_alert"]),
                "intervention_id": p["intervention_id"],
            })
        persons = sorted(persons_by_id.values(), key=lambda x: x["analysis_id"])

        return {
            "version_id": version_id,
            "region_code": region_code,
            "aggregate": {
                "point": result["point"], "ci_low": result["ci_low"],
                "ci_high": result["ci_high"], "quality": result["quality"],
                "n_readings": result["n_readings"],
                "n_households": result["n_households"],
                "n_person_days": result["n_person_days"],
            },
            "data_quality": {
                "readings_total": len(reading_rows),
                "readings_included": quality["included"],
                "readings_excluded": sum(quality["excluded"].values()),
                "excluded_by_reason": quality["excluded"],
                "by_device": quality["by_device"],
            },
            "consent": {
                "persons_total": len(persons),
                "consent_active_at_freeze": sum(1 for x in persons
                                                if x["consent_active_at_freeze"]),
                "consent_currently_active": sum(1 for x in persons
                                                if x["consent_currently_active"]),
                "withdrawn_members": sum(1 for x in persons if x["withdrawn"]),
            },
            "persons": persons,
        }

    def affected_analysis_ids(self, version_id, region_code,
                              symptom_alert=None, consent_lapsed=None):
        """按条件圈定受影响分析编号（不含暴露值判断）。

        - symptom_alert=True：冻结窗口内出现症状预警的成员；
        - consent_lapsed=True：冻结后同意已失效或成员已退出。
        """
        self._require_version_region(version_id, region_code)
        trace = self.region_trace(version_id, region_code)
        ids = []
        for p in trace["persons"]:
            if symptom_alert is not None and p["symptom_alert"] != symptom_alert:
                continue
            lapsed = (not p["consent_currently_active"]) or p["withdrawn"]
            if consent_lapsed is not None and lapsed != consent_lapsed:
                continue
            ids.append(p["analysis_id"])
        return {"version_id": version_id, "region_code": region_code,
                "analysis_ids": ids, "count": len(ids)}


def _today():
    import time
    return time.strftime("%Y-%m-%d", time.gmtime())
