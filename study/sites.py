"""匿名场所与控烟干预版本（分析库）。

场所只保存匿名标签（如 site_type + 序号哈希），不保存任何能定位到
具体家庭或住户的信息。干预版本带生效日期，用于切分政策前后人时。
"""

from .errors import ConflictError, NotFoundError, ValidationError

SITE_TYPES = ("home", "restaurant", "market", "workplace", "transit", "outdoor", "other")


class SiteService:
    def __init__(self, conn, lock):
        self.conn = conn
        self.lock = lock

    def _require_region(self, region_code):
        if self.conn.execute(
            "SELECT 1 FROM regions WHERE region_code=?", (region_code,)
        ).fetchone() is None:
            raise ValidationError(f"地区不在 1..204 覆盖范围内: {region_code}")

    def register_site(self, site_id, region_code, site_type, anonymized_label):
        if site_type not in SITE_TYPES:
            raise ValidationError(f"site_type 必须是 {SITE_TYPES} 之一")
        self._require_region(region_code)
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO sites VALUES (?,?,?,?)",
                    (site_id, region_code, site_type, anonymized_label),
                )
            except Exception as exc:
                raise ConflictError(f"场所已存在: {site_id}") from exc
        return {"site_id": site_id, "region_code": region_code,
                "site_type": site_type, "anonymized_label": anonymized_label}

    def register_intervention(self, intervention_id, region_code, policy_version,
                              effective_date, site_id=None, description=""):
        self._require_region(region_code)
        with self.lock:
            if site_id is not None and self.conn.execute(
                "SELECT 1 FROM sites WHERE site_id=?", (site_id,)
            ).fetchone() is None:
                raise NotFoundError(f"场所不存在: {site_id}")
            dup = self.conn.execute(
                "SELECT 1 FROM interventions WHERE region_code=? AND policy_version=?",
                (region_code, policy_version),
            ).fetchone()
            if dup:
                raise ConflictError(f"该地区政策版本已存在: {policy_version}")
            try:
                self.conn.execute(
                    "INSERT INTO interventions VALUES (?,?,?,?,?,?)",
                    (intervention_id, region_code, site_id, policy_version,
                     effective_date, description),
                )
            except Exception as exc:
                raise ConflictError(f"干预版本已存在: {intervention_id}") from exc
        return {"intervention_id": intervention_id, "policy_version": policy_version,
                "effective_date": effective_date}

    def intervention_active_on(self, region_code, day, site_id=None):
        """某日生效的最新干预版本（政策生效日当天起算）。"""
        sql = (
            "SELECT * FROM interventions WHERE region_code=? AND effective_date<=? "
        )
        params = [region_code, day]
        if site_id is not None:
            sql += "AND (site_id=? OR site_id IS NULL) "
            params.append(site_id)
        sql += "ORDER BY effective_date DESC, intervention_id"
        return self.conn.execute(sql, params).fetchone()
