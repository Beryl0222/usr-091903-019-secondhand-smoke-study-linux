"""暴露估算与版本冻结。

一次冻结版本（estimate version）固化：
- 方法名与参数（含 bootstrap 随机种子与次数，保证可复算）
- 权重表（场所类型权重 / 成员身份权重，用于加权均值）
- 研究窗口、创建人与创建时间
- 204 个地区的点估计、不确定区间、权重合计、样本量与质量标记
- 血缘：进入/未进入估算的每条读数及原因，每个人时段及同意与症状状态

冻结后版本不可修改；freeze_hash 覆盖方法、权重与全部地区结果，
可随时 verify_freeze 复算比对。
"""

import hashlib
import json
import random
import time

from .config import REGION_COUNT
from .errors import (ImmutableVersionError, NotFoundError, StudyError,
                     ValidationError)
from .sensors import _midpoint

DEFAULT_WEIGHTS = {
    # 公共场所类型权重（暴露时长差异）
    "site_type": {
        "restaurant": 1.3, "market": 1.1, "workplace": 1.0,
        "transit": 0.9, "outdoor": 0.6, "home": 1.2, "other": 1.0,
    },
    # 家庭成员身份权重（妇女、儿童为重点保护人群）
    "member_role": {
        "woman": 1.25, "child": 1.35, "man": 1.0, "elder": 1.1, "other": 1.0,
    },
}

DEFAULT_METHOD = {
    "estimator": "weighted_mean",
    "bootstrap": {"seed": 20260919, "n_boot": 500, "ci": 0.95},
    "min_readings_ok": 3,
}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _all_regions(conn):
    return [r["region_code"] for r in conn.execute(
        "SELECT region_code FROM regions ORDER BY seq")]


class EstimationService:
    def __init__(self, analysis_conn, alock, identity_service, sensor_service):
        self.conn = analysis_conn
        self.lock = alock
        self.ids = identity_service
        self.sensors = sensor_service

    # ------------------------------------------------------------------
    def create_frozen_estimate(self, actor, version_id, title, period_start,
                               period_end, weights=None, method=None):
        if period_end < period_start:
            raise ValidationError("period_end 早于 period_start")
        weights = weights or DEFAULT_WEIGHTS
        method = method or DEFAULT_METHOD
        self._validate_weights(weights)

        regions = _all_regions(self.conn)
        if len(regions) != REGION_COUNT:
            raise StudyError(f"覆盖范围异常：应有 {REGION_COUNT} 个地区，实际 {len(regions)}",
                             status=500)

        created_at = _now()
        with self.lock:
            if self.conn.execute(
                "SELECT 1 FROM estimate_versions WHERE version_id=?", (version_id,)
            ).fetchone():
                raise ImmutableVersionError(f"估算版本已存在且冻结: {version_id}")

            region_rows = []
            reading_lineage = []
            person_lineage = []
            for region in regions:
                row, rl, pl = self._estimate_region(
                    region, period_start, period_end, weights, method)
                region_rows.append(row)
                reading_lineage.extend(rl)
                person_lineage.extend(pl)

            self.conn.execute("BEGIN")
            try:
                # 先写结果（不含 hash），再算覆盖全部内容的冻结哈希
                self.conn.execute(
                    "INSERT INTO estimate_versions(version_id, title, method_name,"
                    " method_params, weights_json, period_start, period_end, created_by,"
                    " created_at, region_count, frozen, freeze_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,'')",
                    (version_id, title, method["estimator"],
                     json.dumps(method, ensure_ascii=False, sort_keys=True),
                     json.dumps(weights, ensure_ascii=False, sort_keys=True),
                     period_start, period_end, actor, created_at, len(regions)),
                )
                self.conn.executemany(
                    "INSERT INTO estimate_region_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(version_id, r["region_code"], r["point"], r["ci_low"],
                      r["ci_high"], r["weight"], r["n_readings"], r["n_households"],
                      r["n_person_days"], r["quality"]) for r in region_rows],
                )
                self.conn.executemany(
                    "INSERT INTO estimate_lineage_readings VALUES (?,?,?,?,?,?,?)",
                    [(version_id, x["region_code"], x["reading_id"],
                      x["device_serial"], x["session_id"], x["included"], x["reason"])
                     for x in reading_lineage],
                )
                self.conn.executemany(
                    "INSERT INTO estimate_lineage_persons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(version_id, x["region_code"], x["analysis_id"], x["age_band"],
                      x["role"], x["person_days"], x["consent_id"], x["consent_version"],
                      x["consent_active"], x["symptom_alert"], x["intervention_id"])
                     for x in person_lineage],
                )
                freeze_hash = self._compute_hash(
                    version_id, title, method, weights, period_start, period_end,
                    actor, created_at, region_rows)
                self.conn.execute(
                    "UPDATE estimate_versions SET freeze_hash=? WHERE version_id=?",
                    (freeze_hash, version_id),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        self.ids.audit(actor, "estimate.freeze",
                       f"{version_id} regions={len(regions)} period={period_start}..{period_end}")
        return {"version_id": version_id, "region_count": len(regions),
                "regions_ok": sum(1 for r in region_rows if r["quality"] == "ok"),
                "regions_no_data": sum(1 for r in region_rows if r["quality"] == "no_data"),
                "freeze_hash": freeze_hash}

    # ------------------------------------------------------------------
    def _estimate_region(self, region, start, end, weights, method):
        observations = []  # (value, weight, reading_id, device_serial, session_id)
        reading_lineage = []

        # 1) 场所设备读数（按场所归属地区）
        rows = self.conn.execute(
            "SELECT r.*, d.site_id, s.site_type FROM readings r "
            "JOIN devices d ON d.device_serial=r.device_serial "
            "JOIN sites s ON s.site_id=d.site_id "
            "WHERE s.region_code=? AND date(substr(r.window_start,1,10)) BETWEEN ? AND ?",
            (region, start, end),
        ).fetchall()
        for r in rows:
            decision = self._reading_decision(r)
            if decision[0] == "included":
                value = self.sensors.calibrated_value(r)
                if value is None:  # 入库后校准被作废：保留读数，排除出估算
                    decision = ("excluded", "calibration_invalid")
                else:
                    w = weights["site_type"].get(r["site_type"], 1.0)
                    observations.append((value, w, r["reading_id"], r["device_serial"],
                                         r["session_id"]))
            reading_lineage.append({
                "region_code": region, "reading_id": r["reading_id"],
                "device_serial": r["device_serial"], "session_id": r["session_id"],
                "included": 1 if decision[0] == "included" else 0,
                "reason": decision[1],
            })

        # 2) 家庭设备读数（按采样窗口中点当天家庭所在地区归属）
        hh_rows = self.conn.execute(
            "SELECT r.* FROM readings r JOIN devices d ON d.device_serial=r.device_serial "
            "WHERE d.assign_type='household' AND d.analysis_id IS NOT NULL "
            "AND date(substr(r.window_start,1,10)) BETWEEN ? AND ?",
            (start, end),
        ).fetchall()
        for r in hh_rows:
            mid = _midpoint(r["window_start"], r["window_end"])
            analysis_id = self._device_analysis_id(r["device_serial"])
            if analysis_id is None:
                continue
            try:
                link = self.ids.resolve_analysis_id(analysis_id)
            except NotFoundError:
                # 设备绑定的分析编号在身份库无对应（数据异常）：保留读数，
                # 但不归属任何地区、不进入估算。
                reading_lineage.append({
                    "region_code": region, "reading_id": r["reading_id"],
                    "device_serial": r["device_serial"], "session_id": r["session_id"],
                    "included": 0, "reason": "unresolved_analysis_id",
                })
                continue
            home_region = self.ids.region_on(link["household_id"], mid)
            if home_region != region:
                continue
            decision = self._reading_decision(r)
            if decision[0] == "included":
                value = self.sensors.calibrated_value(r)
                if value is None:
                    decision = ("excluded", "calibration_invalid")
                else:
                    w = weights["member_role"].get(link["role"], 1.0)
                    observations.append((value, w, r["reading_id"], r["device_serial"],
                                         r["session_id"]))
            reading_lineage.append({
                "region_code": region, "reading_id": r["reading_id"],
                "device_serial": r["device_serial"], "session_id": r["session_id"],
                "included": 1 if decision[0] == "included" else 0,
                "reason": decision[1],
            })

        # 3) 人时段（已按同意/搬家/退出/政策切分）
        person_rows = self.conn.execute(
            "SELECT * FROM person_periods WHERE region_code=? "
            "AND start_date<=? AND end_date>=?",
            (region, end, start),
        ).fetchall()
        person_days = 0
        households = set()
        person_lineage = []
        for p in person_rows:
            overlap_days = self._overlap_days(p["start_date"], p["end_date"], start, end)
            if overlap_days <= 0:
                continue
            person_days += overlap_days
            link = self.ids.resolve_analysis_id(p["analysis_id"])
            households.add(link["household_id"])
            alert = 1 if self.ids.symptom_alert(link["member_id"], start, end) else 0
            person_lineage.append({
                "region_code": region, "analysis_id": p["analysis_id"],
                "age_band": p["age_band"], "role": p["role"],
                "person_days": overlap_days, "consent_id": p["consent_id"],
                "consent_version": p["consent_version"], "consent_active": 1,
                "symptom_alert": alert, "intervention_id": p["intervention_id"],
            })

        point, ci_low, ci_high, total_w = self._weighted_statistics(observations, method)
        n = len(observations)
        quality = ("ok" if n >= method["min_readings_ok"]
                   else "sparse" if n > 0 else "no_data")
        row = {
            "region_code": region, "point": point, "ci_low": ci_low, "ci_high": ci_high,
            "weight": round(total_w, 6), "n_readings": n,
            "n_households": len(households), "n_person_days": person_days,
            "quality": quality,
        }
        return row, reading_lineage, person_lineage

    def _device_analysis_id(self, device_serial):
        r = self.conn.execute(
            "SELECT analysis_id FROM devices WHERE device_serial=?", (device_serial,)
        ).fetchone()
        return r["analysis_id"] if r else None

    @staticmethod
    def _reading_decision(reading):
        """估算时点复核读数是否可进入正式估算。数据保留，原因留痕。"""
        if not reading["valid_for_estimate"]:
            return ("excluded", reading["exclude_reason"] or "invalid_at_ingest")
        return ("included", None)

    @staticmethod
    def _overlap_days(s1, e1, s2, e2):
        import datetime
        lo = max(datetime.date.fromisoformat(s1), datetime.date.fromisoformat(s2))
        hi = min(datetime.date.fromisoformat(e1), datetime.date.fromisoformat(e2))
        return (hi - lo).days + 1 if hi >= lo else 0

    @staticmethod
    def _weighted_statistics(observations, method):
        if not observations:
            return None, None, None, 0.0
        values = [o[0] for o in observations]
        weights = [o[1] for o in observations]
        total_w = sum(weights)
        point = sum(v * w for v, w in zip(values, weights)) / total_w

        bs = method.get("bootstrap", {})
        n_boot = bs.get("n_boot", 0)
        if n_boot <= 1 or len(values) < 2:
            return round(point, 4), round(point, 4), round(point, 4), total_w
        rng = random.Random(bs.get("seed", 0))
        n = len(values)
        boots = []
        for _ in range(n_boot):
            sw = sv = 0.0
            for _j in range(n):
                idx = rng.randrange(n)
                sw += weights[idx]
                sv += values[idx] * weights[idx]
            boots.append(sv / sw)
        boots.sort()
        ci = bs.get("ci", 0.95)
        alpha = (1 - ci) / 2
        lo = boots[min(n_boot - 1, int(alpha * n_boot))]
        hi = boots[min(n_boot - 1, int((1 - alpha) * n_boot))]
        return round(point, 4), round(lo, 4), round(hi, 4), total_w

    @staticmethod
    def _validate_weights(weights):
        if set(weights) != {"site_type", "member_role"}:
            raise ValidationError("weights 必须包含 site_type 与 member_role 两组")
        for group in weights.values():
            if not isinstance(group, dict) or not group:
                raise ValidationError("权重组必须是非空映射")
            for k, v in group.items():
                if not isinstance(v, (int, float)) or v <= 0:
                    raise ValidationError(f"权重必须为正数: {k}={v}")

    # ------------------------------------------------------------------
    def get_version(self, version_id):
        v = self.conn.execute(
            "SELECT * FROM estimate_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if v is None:
            raise NotFoundError(f"冻结版本不存在: {version_id}")
        result = dict(v)
        result["method_params"] = json.loads(v["method_params"])
        result["weights"] = json.loads(v["weights_json"])
        del result["weights_json"]
        return result

    def list_versions(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT version_id, title, period_start, period_end, created_by,"
            " created_at, region_count, freeze_hash FROM estimate_versions "
            "ORDER BY created_at")]

    def get_region_results(self, version_id, quality=None):
        self.get_version(version_id)
        sql = "SELECT * FROM estimate_region_results WHERE version_id=?"
        params = [version_id]
        if quality:
            sql += " AND quality=?"
            params.append(quality)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY region_code", params)]

    def update_version(self, *_args, **_kwargs):
        raise ImmutableVersionError("估算版本已冻结，不允许修改；请创建新版本")

    def verify_freeze(self, version_id):
        """复算 freeze_hash，与冻结时记录比对。"""
        v = self.get_version(version_id)
        rows = self.get_region_results(version_id)
        payload = self._freeze_payload(
            version_id, v["title"], v["method_params"], v["weights"],
            v["period_start"], v["period_end"], v["created_by"], v["created_at"], rows)
        actual = hashlib.sha256(payload).hexdigest()
        return {"version_id": version_id, "ok": actual == v["freeze_hash"],
                "stored_hash": v["freeze_hash"], "recomputed_hash": actual}

    def _compute_hash(self, version_id, title, method, weights, start, end,
                      actor, created_at, rows):
        payload = self._freeze_payload(version_id, title, method, weights, start,
                                       end, actor, created_at, rows)
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _freeze_payload(version_id, title, method, weights, start, end,
                        actor, created_at, rows):
        canonical = {
            "version_id": version_id, "title": title, "method": method,
            "weights": weights, "period": [start, end], "actor": actor,
            "created_at": created_at,
            "regions": [
                {k: r[k] for k in ("region_code", "point", "ci_low", "ci_high",
                                   "weight", "n_readings", "n_households",
                                   "n_person_days", "quality")}
                for r in sorted(rows, key=lambda r: r["region_code"])
            ],
        }
        return json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
