"""覆盖 204 个地区的暴露估算与不可变冻结。

方法（随冻结逐字保存，可复算）：

- 分地区、分政策期（pre/post）、分年龄段计算实测人天 PM2.5 均值；
  只有校准有效读数支撑的人天行（``measured=True``）进入均值；
- **粗均值**直接对实测人天求均值，人口构成变化会混入结果；
- **直接标准化均值** = Σ 年龄段标准人口权重 × 段均值，权重在估算时
  固定并写入冻结，从而把“真实下降”与“人口（年龄构成）变化”分开；
- 不确定区间按各段均值方差合成（Welch–Satterthwaite 自由度），
  数据不足的地区标记 ``insufficient_data``，估计值留空但仍列入冻结；
- 冻结保存方法、权重、区间公式、逐地区结果与输入行指纹，
  之后不可修改；输入行为只增快照，指纹可随时复核。
"""

import hashlib
import json
import math
import threading
from collections import defaultdict

from study.catalog import AGE_BANDS, REGION_COUNT
from study.errors import Conflict, NotFound, ValidationError
from study.util import iso, new_id, parse_date

Z_95 = 1.959964


def t_critical(df):
    """双侧 95% t 分位数的 Cornish–Fisher 近似，df 小时比 z 更保守。"""
    if df <= 0:
        return float("inf")
    if df >= 1000:
        return Z_95
    z = Z_95
    return (z
            + (z ** 3 + z) / (4 * df)
            + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)
            + (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z)
            / (384 * df ** 3))


def _mean_var(values):
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, variance


def _stratum_interval(weighted_variances):
    """合成标准误与 Welch–Satterthwaite 自由度的 95% 区间半宽。"""
    se2 = sum(w2 * v for w2, v, _df in weighted_variances)
    se = math.sqrt(se2)
    denominator = sum((w2 * v) ** 2 / df
                      for w2, v, df in weighted_variances if df > 0 and v > 0)
    dof = (se2 ** 2 / denominator) if denominator > 0 else float("inf")
    return se, t_critical(dof) * se, dof


METHOD_SPEC = {
    "name": "direct_standardization",
    "version": "1.0",
    "strata": list(AGE_BANDS),
    "mean_rule": "各年龄段内对校准有效的实测人天 PM2.5 求算术均值",
    "standardized_rule": "Σ(标准人口权重_b × 段均值_b)",
    "crude_rule": "地区/政策期内全部实测人天的算术均值（未调整年龄构成）",
    "interval": "段均值方差按权重合成，95% 区间用 Welch–Satterthwaite 自由度 t 近似",
    "change_rule": "post 标准化均值 − pre 标准化均值，区间按两期独立合成",
    "ci_level": 0.95,
}


class EstimateStore:
    def __init__(self, catalog, vault, sensors, exposure, audit):
        self._catalog = catalog
        self._vault = vault
        self._sensors = sensors
        self._exposure = exposure
        self._audit = audit
        self._lock = threading.Lock()
        self._runs = {}
        self._freezes = {}

    # ---- 估算 ----------------------------------------------------------
    def run(self, actor, window_start, window_end, weights=None, label=None):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"analyst", "admin"}:
            from study.errors import PermissionDenied
            raise PermissionDenied(f"角色 {role} 无权运行估算")
        start = parse_date(window_start)
        end = parse_date(window_end)
        if end <= start:
            raise ValidationError("估算窗口结束日必须晚于开始日")
        weights = self._validate_weights(weights)

        contributions = self._exposure.contributions("analyst")
        contributions = [r for r in contributions
                         if start <= parse_date(r["date"]) < end]
        venue_days = [r for r in self._exposure.venue_days("analyst")
                      if start <= parse_date(r["date"]) < end]

        region_codes = [r["region_code"] for r in self._catalog.list_regions()]
        if len(region_codes) != REGION_COUNT:
            raise Conflict(f"目录地区数异常：{len(region_codes)} ≠ {REGION_COUNT}")

        # (region, phase, age_band) -> 实测人天 pm 值
        strata = defaultdict(list)
        day_counts = defaultdict(lambda: {"measured": 0, "total": 0})
        for row in contributions:
            key = (row["region_code"], row["policy_phase"])
            day_counts[key]["total"] += 1
            if row["measured"]:
                day_counts[key]["measured"] += 1
                strata[key + (row["age_band"],)].append(row["pm25_mean"])

        results = []
        for region in region_codes:
            phases = {}
            for phase in ("pre", "post"):
                phases[phase] = self._estimate_phase(
                    region, phase, weights, strata, day_counts)
            change = self._change(phases)
            results.append({
                "region_code": region,
                "phases": phases,
                "change": change,
            })

        quality = self._quality_summary(region_codes, venue_days, start, end)

        # 进入估算的输入行索引（只增存储），用于冻结后复核与伦理溯源。
        input_index = {
            "contributions": sorted(
                ({"id": r["contribution_id"], "measured": r["measured"],
                  "pm25_mean": r["pm25_mean"]} for r in contributions),
                key=lambda x: x["id"]),
            "venue_days": sorted(
                ({"id": r["envday_id"], "pm25_mean": r["pm25_mean"]}
                 for r in venue_days),
                key=lambda x: x["id"]),
        }
        run = {
            "run_id": None,
            "label": label,
            "window": {"start": iso(start), "end": iso(end)},
            "method": METHOD_SPEC,
            "weights": weights,
            "regions": results,
            "quality": quality,
            "input_index": input_index,
            "regions_estimated": sum(
                1 for r in results
                if r["phases"]["pre"]["status"] == "ok"
                or r["phases"]["post"]["status"] == "ok"),
            "regions_insufficient": sum(
                1 for r in results
                if r["phases"]["pre"]["status"] != "ok"
                and r["phases"]["post"]["status"] != "ok"),
            "region_total": REGION_COUNT,
        }
        with self._lock:
            run["run_id"] = new_id("RUN", self._runs)
            self._runs[run["run_id"]] = run
        self._audit.append(actor, "estimate_run",
                           run_id=run["run_id"], window_start=iso(start),
                           window_end=iso(end),
                           regions_estimated=run["regions_estimated"])
        return self._run_view(run)

    @staticmethod
    def _validate_weights(weights):
        if weights is None:
            # 缺省均匀标准人口，仅为联调默认；正式冻结应显式传入。
            weights = {band: 1.0 / len(AGE_BANDS) for band in AGE_BANDS}
        missing = [b for b in AGE_BANDS if b not in weights]
        if missing:
            raise ValidationError("标准人口权重缺少年龄段", missing=missing)
        extra = [b for b in weights if b not in AGE_BANDS]
        if extra:
            raise ValidationError("标准人口权重含未知年龄段", extra=extra)
        cleaned = {b: float(weights[b]) for b in AGE_BANDS}
        if any(v < 0 for v in cleaned.values()):
            raise ValidationError("标准人口权重不能为负")
        total = sum(cleaned.values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValidationError(f"标准人口权重之和应为 1，实际为 {total}")
        return cleaned

    def _estimate_phase(self, region, phase, weights, strata, day_counts):
        counts = day_counts.get((region, phase), {"measured": 0, "total": 0})
        stratum_stats = {}
        weighted_variances = []
        missing_bands = []
        for band in AGE_BANDS:
            values = strata.get((region, phase, band), [])
            if not values:
                if weights[band] > 0:
                    missing_bands.append(band)
                continue
            mean, variance = _mean_var(values)
            stratum_stats[band] = {
                "mean": round(mean, 3),
                "n_days": len(values),
                "variance": round(variance, 4),
            }
            if len(values) >= 2:
                weighted_variances.append(
                    (weights[band] ** 2, variance / len(values),
                     len(values) - 1))

        all_values = [v for band in AGE_BANDS
                      for v in strata.get((region, phase, band), [])]
        crude = None
        if all_values:
            crude_mean, crude_var = _mean_var(all_values)
            n = len(all_values)
            half = (t_critical(n - 1) * math.sqrt(crude_var / n)
                    if n >= 2 else None)
            crude = {
                "mean": round(crude_mean, 3),
                "ci_lower": round(crude_mean - half, 3)
                if half is not None else None,
                "ci_upper": round(crude_mean + half, 3)
                if half is not None else None,
                "n_days": n,
            }

        if missing_bands:
            return {
                "status": "insufficient_data",
                "missing_bands": missing_bands,
                "strata": stratum_stats,
                "crude": crude,
                "person_days_total": counts["total"],
                "person_days_measured": counts["measured"],
            }

        standardized = sum(weights[b] * stratum_stats[b]["mean"]
                           for b in stratum_stats)
        se, half_width, dof = _stratum_interval(weighted_variances)
        return {
            "status": "ok",
            "standardized_mean": round(standardized, 3),
            "ci_lower": round(standardized - half_width, 3),
            "ci_upper": round(standardized + half_width, 3),
            "se": round(se, 4),
            "df": None if math.isinf(dof) else round(dof, 2),
            "strata": stratum_stats,
            "crude": crude,
            "person_days_total": counts["total"],
            "person_days_measured": counts["measured"],
        }

    def _change(self, phases):
        """对比标准化（真实下降口径）与粗均值（含人口构成变化口径）。"""
        std = {p: phases[p].get("standardized_mean") for p in ("pre", "post")}
        if std["pre"] is None or std["post"] is None:
            return {"status": "insufficient_data"}
        pre_se2 = phases["pre"]["se"] ** 2
        post_se2 = phases["post"]["se"] ** 2
        delta_se = math.sqrt(pre_se2 + post_se2)
        if pre_se2 + post_se2 == 0:
            # 各段内读数完全一致：均值无抽样变异，区间退化为点。
            half = 0.0
        else:
            pre_df = phases["pre"]["df"]
            post_df = phases["post"]["df"]
            denom = ((pre_se2 ** 2 / pre_df) if pre_df and pre_se2 else 0) + \
                    ((post_se2 ** 2 / post_df) if post_df and post_se2 else 0)
            dof = ((pre_se2 + post_se2) ** 2 / denom) if denom > 0 \
                else float("inf")
            half = t_critical(dof) * delta_se
        delta_std = std["post"] - std["pre"]
        delta_crude = None
        if phases["pre"]["crude"] and phases["post"]["crude"]:
            delta_crude = round(
                phases["post"]["crude"]["mean"] - phases["pre"]["crude"]["mean"],
                3)
        composition_driven = (
            delta_crude is not None
            and abs(delta_crude - round(delta_std, 3)) >= 0.5
        )
        return {
            "status": "ok",
            "std_change": round(delta_std, 3),
            "ci_lower": round(delta_std - half, 3),
            "ci_upper": round(delta_std + half, 3),
            "crude_change": delta_crude,
            "composition_driven": composition_driven,
        }

    def _quality_summary(self, region_codes, venue_days, start, end):
        """汇总每地区传感器质量：有效/失效读数、缺测窗口、场所日数。"""
        venue_bucket = defaultdict(lambda: {
            "valid_readings": 0, "invalid_readings": 0,
            "missing_windows": 0, "venue_days": 0})
        for day in venue_days:
            row = venue_bucket[day["region_code"]]
            row["valid_readings"] += day["valid_readings"]
            row["invalid_readings"] += day["invalid_readings"]
            row["missing_windows"] += day["missing_windows"]
            row["venue_days"] += 1

        # 家庭部署的质量按家庭所属社区归入地区。
        household_bucket = defaultdict(lambda: {
            "valid_readings": 0, "invalid_readings": 0,
            "missing_windows": 0})
        for deployment in self._sensors.deployments():
            if deployment["location_kind"] != "household":
                continue
            household = self._vault.get_household(deployment["location_ref"])
            community = self._catalog.get_community(household["community_id"])
            counts = self._sensors.quality_counts(deployment["location_ref"])
            row = household_bucket[community["region_code"]]
            row["valid_readings"] += counts["valid_readings"]
            row["invalid_readings"] += counts["invalid_readings"]
            row["missing_windows"] += counts["missing_windows"]

        summary = {}
        for region in region_codes:
            v = venue_bucket.get(region, {
                "valid_readings": 0, "invalid_readings": 0,
                "missing_windows": 0, "venue_days": 0})
            h = household_bucket.get(region, {
                "valid_readings": 0, "invalid_readings": 0,
                "missing_windows": 0})
            summary[region] = {
                "valid_readings": v["valid_readings"] + h["valid_readings"],
                "invalid_readings": v["invalid_readings"] + h["invalid_readings"],
                "missing_windows": v["missing_windows"] + h["missing_windows"],
                "venue_days": v["venue_days"],
            }
        return summary

    # ---- 冻结 ----------------------------------------------------------
    def freeze(self, actor, run_id=None, label=None):
        """把某次估算（方法/权重/区间/逐地区结果）冻结为不可变记录。"""
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"analyst", "admin"}:
            from study.errors import PermissionDenied
            raise PermissionDenied(f"角色 {role} 无权冻结估算")
        with self._lock:
            if run_id is None:
                if not self._runs:
                    raise NotFound("还没有可冻结的估算运行")
                run_id = sorted(self._runs)[-1]
            run = self._runs.get(run_id)
            if run is None:
                raise NotFound(f"估算运行不存在：{run_id}")
            freeze_id = new_id("FZ", self._freezes)
            fingerprint = self._fingerprint(run)
            record = {
                "freeze_id": freeze_id,
                "label": label or run.get("label"),
                "run_id": run_id,
                "window": run["window"],
                "method": run["method"],
                "weights": run["weights"],
                "regions": run["regions"],
                "regions_by_code": {r["region_code"]: r for r in run["regions"]},
                "quality": run["quality"],
                "input_index": run["input_index"],
                "region_total": run["region_total"],
                "regions_estimated": run["regions_estimated"],
                "regions_insufficient": run["regions_insufficient"],
                "input_fingerprint": fingerprint,
            }
            record["content_hash"] = self._content_hash(record)
            self._freezes[freeze_id] = record
        self._audit.append(actor, "estimate_frozen", freeze_id=freeze_id,
                           run_id=run_id, fingerprint=fingerprint)
        return self._freeze_view(record)

    def get_freeze(self, actor, freeze_id):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"analyst", "ethics", "admin", "community"}:
            from study.errors import PermissionDenied
            raise PermissionDenied(f"角色 {role} 无权读取冻结")
        with self._lock:
            record = self._freezes.get(freeze_id)
            if record is None:
                raise NotFound(f"冻结不存在：{freeze_id}")
            return self._freeze_view(record)

    def list_freezes(self, actor):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"analyst", "ethics", "admin", "community"}:
            from study.errors import PermissionDenied
            raise PermissionDenied(f"角色 {role} 无权列出冻结")
        with self._lock:
            return [{
                "freeze_id": r["freeze_id"],
                "label": r["label"],
                "run_id": r["run_id"],
                "window": r["window"],
                "region_total": r["region_total"],
                "regions_estimated": r["regions_estimated"],
                "regions_insufficient": r["regions_insufficient"],
                "input_fingerprint": r["input_fingerprint"],
                "content_hash": r["content_hash"],
            } for r in sorted(self._freezes.values(),
                              key=lambda r: r["freeze_id"])]

    def verify_freeze(self, actor, freeze_id):
        """复核冻结内容哈希是否与保存时一致（防篡改）。"""
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"analyst", "ethics", "admin"}:
            from study.errors import PermissionDenied
            raise PermissionDenied(f"角色 {role} 无权复核冻结")
        with self._lock:
            record = self._freezes.get(freeze_id)
            if record is None:
                raise NotFound(f"冻结不存在：{freeze_id}")
            stored = record["content_hash"]
            rebuilt = self._content_hash(
                {k: v for k, v in record.items() if k != "content_hash"})
        ok = stored == rebuilt
        self._audit.append(actor, "freeze_verified",
                           freeze_id=freeze_id, intact=ok)
        return {"freeze_id": freeze_id, "intact": ok,
                "content_hash": stored}

    @staticmethod
    def _fingerprint(run):
        """对进入估算的输入行集合取指纹（只增存储，故长期可复核）。"""
        payload = {
            "window": run["window"],
            "weights": run["weights"],
            "contributions": run["input_index"]["contributions"],
            "venue_days": run["input_index"]["venue_days"],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _content_hash(record):
        blob = json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ---- 视图 ----------------------------------------------------------
    def _run_view(self, run):
        return {k: v for k, v in run.items()}

    def _freeze_view(self, record):
        return {k: v for k, v in record.items()}

    def get_run(self, actor, run_id):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in {"analyst", "ethics", "admin"}:
            from study.errors import PermissionDenied
            raise PermissionDenied(f"角色 {role} 无权读取运行")
        run = self._runs.get(run_id)
        if run is None:
            raise NotFound(f"估算运行不存在：{run_id}")
        return self._run_view(run)
