"""人时截断（同意/搬家/退出/政策）与症状随访测试。"""

import unittest

from test_support import build_scenario, materialized


class ExposureTruncationTest(unittest.TestCase):
    def _run(self):
        ctx = build_scenario()
        materialized(ctx)
        return ctx

    def test_person_days_split_by_policy_phase(self):
        ctx = self._run()
        rows = ctx["app"].exposure.contributions("analyst",
                                                 community_id="C001")
        # 每人政策前 31 天（1月）+ 28 天（2月）= 59 天；
        # 政策后 30（4月）；窗口在 5-01 截止，3 月 31 天也在政策后。
        pre = [r for r in rows if r["policy_phase"] == "pre"]
        post = [r for r in rows if r["policy_phase"] == "post"]
        self.assertEqual(len(pre), 59 * 2)
        self.assertEqual(len(post), (31 + 30) * 2)

    def test_consent_gap_excludes_days(self):
        # 生命周期事件先确定，再物化：已物化行只增，不追溯改写。
        ctx = build_scenario()
        app = ctx["app"]
        app.vault.end_consent("field", ctx["hh1"], "2026-02-01")
        app.vault.grant_consent("field", ctx["hh1"], "2026-03-15")
        materialized(ctx)
        rows = app.exposure.contributions("analyst",
                                          community_id="C001")
        a1_days = {r["date"] for r in rows if r["analysis_id"] == ctx["a1"]}
        self.assertNotIn("2026-02-10", a1_days)
        self.assertNotIn("2026-03-14", a1_days)
        self.assertIn("2026-03-15", a1_days)
        self.assertIn("2026-01-31", a1_days)
        # 另一名成员不受影响。
        a2_days = {r["date"] for r in rows if r["analysis_id"] == ctx["a2"]}
        self.assertIn("2026-02-10", a2_days)

    def test_withdrawal_truncates_person_days(self):
        ctx = build_scenario()
        app = ctx["app"]
        app.vault.withdraw_member("field", ctx["a1"], "2026-02-15")
        materialized(ctx)
        rows = app.exposure.contributions("analyst",
                                          community_id="C001")
        a1_days = [r for r in rows if r["analysis_id"] == ctx["a1"]]
        self.assertTrue(all(r["date"] < "2026-02-15" for r in a1_days))
        self.assertIn("2026-02-14", {r["date"] for r in a1_days})

    def test_move_truncates_to_new_community_by_real_date(self):
        ctx = build_scenario()
        app = ctx["app"]
        app.catalog.add_community("C999", "R099")
        hh_new = app.vault.create_household("field", "C999")[
            "household_id"]
        app.vault.grant_consent("field", hh_new, "2026-01-01")
        app.vault.move_member("field", ctx["a1"], hh_new, "2026-03-10")
        materialized(ctx)
        rows = app.exposure.contributions("analyst")
        a1 = [r for r in rows if r["analysis_id"] == ctx["a1"]]
        self.assertTrue(all(
            (r["date"] < "2026-03-10" and r["community_id"] == "C001")
            or (r["date"] >= "2026-03-10" and r["community_id"] == "C999")
            for r in a1))
        self.assertIn("2026-03-10",
                      {r["date"] for r in a1 if r["community_id"] == "C999"})

    def test_age_band_is_taken_by_date_not_current_age(self):
        ctx = self._run()
        rows = ctx["app"].exposure.contributions("analyst")
        a1 = {r["date"]: r["age_band"] for r in rows
              if r["analysis_id"] == ctx["a1"]}
        # 2023-01-01 出生：2026 年内仍不满 3 岁。
        self.assertEqual(a1["2026-04-01"], "under_5")

    def test_unmeasured_days_remain_but_flagged(self):
        ctx = build_scenario(with_readings=False)
        materialized(ctx)
        rows = ctx["app"].exposure.contributions("analyst",
                                                 measured_only=True)
        self.assertEqual(rows, [])
        all_rows = ctx["app"].exposure.contributions("analyst")
        self.assertTrue(all(r["measured"] is False for r in all_rows))
        self.assertTrue(all(r["pm25_mean"] is None for r in all_rows))

    def test_materialization_is_append_only(self):
        ctx = self._run()
        app = ctx["app"]
        before = app.exposure.contributions("analyst")
        # 再次物化不新增也不覆盖（窗口内已全部存在）。
        summary = app.exposure.materialize("analyst",
                                           "2026-01-01", "2026-05-01")
        self.assertEqual(summary["person_days"], 0)
        after = app.exposure.contributions("analyst")
        self.assertEqual(before, after)

    def test_missing_venue_day_still_counted_in_quality(self):
        ctx = build_scenario(with_readings=False)
        # 场所机完全无读数；闭合部署后物化。
        for serial in ctx["serials"]:
            ctx["app"].sensors.close_deployment(
                ctx["field"], serial, "2026-05-01")
        ctx["app"].exposure.materialize(
            ctx["analyst"], "2026-01-01", "2026-05-01")
        venue_days = ctx["app"].exposure.venue_days("analyst")
        self.assertTrue(venue_days)
        missing_days = [d for d in venue_days if d["pm25_mean"] is None]
        self.assertTrue(missing_days)
        self.assertTrue(all(d["missing_windows"] >= 1 for d in missing_days))

    def test_venue_day_backfill_supersedes_quality_row(self):
        ctx = build_scenario(with_readings=False)
        app = ctx["app"]
        for serial in ctx["serials"]:
            app.sensors.close_deployment(ctx["field"], serial, "2026-05-01")
        app.exposure.materialize(ctx["analyst"], "2026-01-01", "2026-05-01")
        before = {d["date"]: d for d in app.exposure.venue_days("analyst")
                  if d["venue_id"] == ctx["venue_id"]}
        target = "2026-01-10"
        self.assertIsNone(before[target]["pm25_mean"])
        old_id = before[target]["envday_id"]
        # 补传该场所当日读数。
        app.sensors.ingest_batch(ctx["field"], "SN-3", [{
            "sample_start": f"{target}T00:00:00Z",
            "sample_end": f"{target}T23:59:00Z", "pm25": 33}])
        app.exposure.materialize(ctx["analyst"], "2026-01-01", "2026-05-01")
        after = {d["date"]: d for d in app.exposure.venue_days("analyst")
                 if d["venue_id"] == ctx["venue_id"]}
        self.assertEqual(after[target]["pm25_mean"], 33.0)
        self.assertEqual(after[target]["supersedes"], old_id)
        self.assertNotEqual(after[target]["envday_id"], old_id)

    def test_symptom_outside_consent_window_flagged_but_kept(self):
        ctx = self._run()
        app = ctx["app"]
        app.vault.end_consent("field", ctx["hh1"], "2026-02-01")
        inside = app.exposure.record_symptoms(
            "field", ctx["a1"], "2026-01-20", ["cough"])
        outside = app.exposure.record_symptoms(
            "field", ctx["a1"], "2026-03-20", ["cough"])
        self.assertTrue(inside["in_consent_window"])
        self.assertFalse(outside["in_consent_window"])
        # 两条随访都保留。
        self.assertEqual(
            len(app.exposure.symptom_followups("field", ctx["a1"])), 2)


if __name__ == "__main__":
    unittest.main()
