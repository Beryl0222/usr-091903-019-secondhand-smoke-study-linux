"""人时（person-time）时间线构建。

对每个分析编号，按真实日期把研究窗口切成内部状态恒定的小区间，
状态包括：所在地区（搬家截断）、成员在组状态（入组/退出）、
同意是否有效（授予/撤销截断）、当日生效的控烟干预版本。

规则（均含端点当天）：
- 成员贡献从 enrolled_on 起，到 withdrawn_on 止（退出日后不再贡献）；
- 搬家生效日起人时归新地区，前一天止归旧地区；
- 同意 granted_on 当天生效，revoked_on 当天仍有效、次日起失效；
  无有效同意的日期不产生任何人时；
- 干预政策 effective_date 当天起算。

结果写入分析库 person_periods；正式估算时再把快照复制进冻结血缘。
"""

import datetime

from .sites import SiteService


def _date(value):
    return datetime.date.fromisoformat(value)


def _iso(d):
    return d.isoformat()


def _add_days(value, delta):
    return _iso(_date(value) + datetime.timedelta(days=delta))


class TimelineBuilder:
    def __init__(self, identity_service, analysis_conn, lock):
        self.ids = identity_service
        self.conn = analysis_conn
        self.lock = lock
        self.sites = SiteService(analysis_conn, lock)

    def build(self, period_start, period_end):
        if period_end < period_start:
            raise ValueError("period_end 早于 period_start")
        links = self.ids.all_analysis_links()
        periods = []
        for link in links:
            periods.extend(self._member_periods(link, period_start, period_end))

        with self.lock:
            self.conn.execute(
                "DELETE FROM person_periods WHERE start_date>=? AND end_date<=?",
                (period_start, period_end),
            )
            self.conn.execute("BEGIN")
            try:
                for p in periods:
                    self.conn.execute(
                        "INSERT INTO person_periods VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (p["analysis_id"], p["region_code"], p["intervention_id"],
                         p["start_date"], p["end_date"], p["person_days"],
                         p["consent_id"], p["consent_version"], p["age_band"], p["role"]),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {"period_start": period_start, "period_end": period_end,
                "n_periods": len(periods),
                "n_person_days": sum(p["person_days"] for p in periods)}

    def _member_periods(self, link, period_start, period_end):
        conn = self.ids.conn
        member = conn.execute(
            "SELECT * FROM members WHERE member_id=?", (link["member_id"],)
        ).fetchone()
        if member is None:
            return []

        # 在组窗口：入组日 .. min(退出日, 窗口末)
        m_start = max(member["enrolled_on"], period_start)
        m_end = period_end
        if member["withdrawn_on"]:
            m_end = min(m_end, member["withdrawn_on"])
        if m_start > m_end:
            return []

        household_id = link["household_id"]

        # 搬家边界：取与在组窗口相交的地区段
        moves = conn.execute(
            "SELECT region_code, effective_from, effective_to "
            "FROM household_region_history WHERE household_id=? ORDER BY effective_from",
            (household_id,),
        ).fetchall()

        # 同意边界
        consents = conn.execute(
            "SELECT * FROM consents WHERE household_id=? "
            "AND granted_on<=? AND (revoked_on IS NULL OR revoked_on>=?)",
            (household_id, m_end, m_start),
        ).fetchall()

        # 所有切点（半开区间的起点）：窗口起点、搬家起点、同意授予、
        # 撤销次日、以及成员经过地区的控烟政策生效日
        boundaries = {m_start}
        member_regions = set()
        for mv in moves:
            member_regions.add(mv["region_code"])
            if mv["effective_from"]:
                boundaries.add(max(mv["effective_from"], m_start))
        for c in consents:
            boundaries.add(max(c["granted_on"], m_start))
            if c["revoked_on"]:
                boundaries.add(_add_days(c["revoked_on"], 1))
        for region in member_regions:
            for iv in self.conn.execute(
                "SELECT effective_date FROM interventions WHERE region_code=? "
                "AND effective_date BETWEEN ? AND ?",
                (region, m_start, m_end),
            ).fetchall():
                boundaries.add(iv["effective_date"])
        cuts = sorted(b for b in boundaries if m_start <= b <= m_end)

        periods = []
        for i, seg_start in enumerate(cuts):
            seg_end = (
                min(_add_days(cuts[i + 1], -1), m_end)
                if i + 1 < len(cuts)
                else m_end
            )
            if seg_start > seg_end:
                continue

            region = self.ids.region_on(household_id, seg_start)
            if region is None:
                continue  # 该日无在住地区记录（理论上不应出现）

            consent = self.ids.active_consent(household_id, seg_start)
            if consent is None:
                continue  # 无有效同意：不产生人时

            iv = self.sites.intervention_active_on(region, seg_start)
            days = (_date(seg_end) - _date(seg_start)).days + 1
            periods.append({
                "analysis_id": link["analysis_id"],
                "region_code": region,
                "intervention_id": iv["intervention_id"] if iv else None,
                "start_date": seg_start,
                "end_date": seg_end,
                "person_days": days,
                "consent_id": consent["consent_id"],
                "consent_version": consent["version"],
                "age_band": member["age_band"],
                "role": member["role"],
            })
        return periods

    def periods(self, period_start=None, period_end=None, region_code=None):
        sql = "SELECT * FROM person_periods WHERE 1=1"
        params = []
        if period_start:
            sql += " AND start_date>=?"
            params.append(period_start)
        if period_end:
            sql += " AND end_date<=?"
            params.append(period_end)
        if region_code:
            sql += " AND region_code=?"
            params.append(region_code)
        sql += " ORDER BY analysis_id, start_date"
        return [dict(r) for r in self.conn.execute(sql, params)]
