"""领域规则单元测试：分库、人时截断、传感器去重、冻结、反馈抑制、伦理通知。"""

import os
import tempfile
import unittest

from study.app import StudyApp
from study.config import REGION_COUNT
from study.errors import (ConflictError, ImmutableVersionError, NotFoundError,
                          ValidationError)


class StudyTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shs-test-")
        self.ipath = os.path.join(self.dir, "identity.db")
        self.apath = os.path.join(self.dir, "analysis.db")
        self.app = StudyApp(self.ipath, self.apath)
        self.keys = self.app.init_demo_keys()

    def tearDown(self):
        self.app.close()


class SeparationTest(StudyTestCase):
    def test_analysis_store_has_no_real_identity_columns(self):
        # 分析库只允许出现 analysis_id；member_id/contact_ref 等身份字段
        # 不得越过分库边界。
        schema = self.app.db.analysis.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'").fetchall()
        text = "\n".join(r["sql"] or "" for r in schema)
        self.assertNotIn("member_id", text)
        self.assertNotIn("contact_ref", text)
        self.assertNotIn("pseudonym", text)
        self.assertIn("analysis_id", text)

    def test_identity_store_holds_link_and_analysis_store_does_not(self):
        self.app.identity.register_household("f", "H1", "c", "R001", "ref", "2026-01-01")
        m = self.app.identity.register_member("f", "M1", "H1", "母亲", "18-59",
                                              "woman", "2026-01-01")
        link = self.app.db.identity.execute(
            "SELECT * FROM analysis_links WHERE analysis_id=?", (m["analysis_id"],)
        ).fetchone()
        self.assertEqual(link["member_id"], "M1")
        with self.assertRaises(Exception):
            self.app.db.analysis.execute("SELECT member_id FROM sites").fetchall()


class TimelineTest(StudyTestCase):
    def _enroll(self, hh="H1", region="R001"):
        self.app.identity.register_household("f", hh, "c", region, "ref", "2026-01-01")
        m = self.app.identity.register_member("f", hh + "-M", hh, "p", "18-59",
                                              "woman", "2026-01-01")
        self.app.identity.grant_consent("f", hh + "-C", hh, "air", "v1", "2026-01-01")
        return m

    def test_policy_effective_date_splits_person_time(self):
        m = self._enroll()
        self.app.sites.register_intervention("IV1", "R001", "pol", "2026-03-01")
        summary = self.app.timeline.build("2026-01-01", "2026-06-30")
        self.assertEqual(summary["n_periods"], 2)
        periods = self.app.timeline.periods()
        self.assertIsNone(periods[0]["intervention_id"])
        self.assertEqual(periods[1]["intervention_id"], "IV1")
        self.assertEqual(periods[0]["person_days"], 59)
        self.assertEqual(periods[1]["person_days"], 122)

    def test_move_truncates_contribution_by_real_date(self):
        m = self._enroll(region="R001")
        self.app.identity.record_move("f", "H1", "R002", "2026-04-01")
        self.app.timeline.build("2026-01-01", "2026-06-30")
        r1 = [p for p in self.app.timeline.periods(region_code="R001")]
        r2 = [p for p in self.app.timeline.periods(region_code="R002")]
        self.assertEqual(r1[-1]["end_date"], "2026-03-31")
        self.assertEqual(r2[0]["start_date"], "2026-04-01")
        self.assertEqual(r2[0]["person_days"], 91)

    def test_withdrawal_stops_contribution_on_that_date(self):
        m = self._enroll()
        self.app.identity.withdraw_member("f", "H1-M", "2026-02-15")
        self.app.timeline.build("2026-01-01", "2026-06-30")
        periods = self.app.timeline.periods()
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]["end_date"], "2026-02-15")
        self.assertEqual(periods[0]["person_days"], 46)

    def test_consent_revocation_day_still_active_next_day_excluded(self):
        m = self._enroll()
        self.app.identity.revoke_consent("f", "H1-C", "2026-05-01")
        self.app.timeline.build("2026-01-01", "2026-06-30")
        periods = self.app.timeline.periods()
        self.assertEqual(max(p["end_date"] for p in periods), "2026-05-01")

    def test_no_consent_no_person_time(self):
        self.app.identity.register_household("f", "H9", "c", "R001", "ref", "2026-01-01")
        self.app.identity.register_member("f", "H9-M", "H9", "p", "5-11",
                                          "child", "2026-01-01")
        summary = self.app.timeline.build("2026-01-01", "2026-06-30")
        self.assertEqual(summary["n_person_days"], 0)


class SensorTest(StudyTestCase):
    def _site_device(self, serial="D1", region="R001"):
        self.app.sites.register_site("S1", region, "restaurant", "anon")
        self.app.sensors.register_device(serial, site_id="S1")

    def test_backfill_dedup_by_device_and_window(self):
        self._site_device()
        self.app.sensors.add_calibration(
            "K1", "D1", "2026-01-01T00:00:00Z", "2026-01-01")
        window = {"window_start": "2026-02-01T00:00:00Z",
                  "window_end": "2026-02-01T02:00:00Z", "pm25": 30.0}
        first = self.app.sensors.ingest_batch("D1", [dict(window)])
        again = self.app.sensors.ingest_batch("D1", [{**window, "pm25": 999.0}])
        self.assertEqual((first["accepted"], first["duplicates"]), (1, 0))
        self.assertEqual((again["accepted"], again["duplicates"]), (0, 1))
        # 原值不被重复补传覆盖
        row = self.app.db.analysis.execute(
            "SELECT pm25 FROM readings WHERE device_serial='D1'").fetchone()
        self.assertEqual(row["pm25"], 30.0)

    def test_uncalibrated_reading_retained_but_excluded(self):
        self._site_device(serial="D2")
        result = self.app.sensors.ingest_batch(
            "D2", [{"window_start": "2026-02-01T00:00:00Z",
                    "window_end": "2026-02-01T02:00:00Z", "pm25": 50.0}])
        self.assertEqual(result["excluded"], 1)
        row = self.app.db.analysis.execute(
            "SELECT * FROM readings WHERE device_serial='D2'").fetchone()
        self.assertEqual(row["pm25"], 50.0)
        self.assertEqual(row["valid_for_estimate"], 0)
        self.assertEqual(row["exclude_reason"], "no_calibration")

    def test_invalidated_calibration_excludes_at_estimate_time(self):
        self._site_device(serial="D3")
        self.app.sensors.add_calibration(
            "K3", "D3", "2026-01-01T00:00:00Z", "2026-01-01")
        self.app.sensors.ingest_batch(
            "D3", [{"window_start": "2026-02-01T00:00:00Z",
                    "window_end": "2026-02-01T02:00:00Z", "pm25": 20.0}])
        self.app.sensors.invalidate_calibration("K3")
        row = self.app.db.analysis.execute(
            "SELECT * FROM readings WHERE device_serial='D3'").fetchone()
        self.assertIsNone(self.app.sensors.calibrated_value(row))
        # 原始读数仍保留
        self.assertEqual(row["pm25"], 20.0)

    def test_overlapping_calibration_rejected(self):
        self._site_device(serial="D4")
        self.app.sensors.add_calibration(
            "K4a", "D4", "2026-01-01T00:00:00Z", "2026-01-01", "2026-03-01")
        with self.assertRaises(ConflictError):
            self.app.sensors.add_calibration(
                "K4b", "D4", "2026-02-01T00:00:00Z", "2026-02-01")


class EstimationTest(StudyTestCase):
    def _seed_r001(self):
        self.app.identity.register_household("f", "H1", "c", "R001", "ref", "2026-01-01")
        mother = self.app.identity.register_member(
            "f", "M1", "H1", "母", "18-59", "woman", "2026-01-01")
        child = self.app.identity.register_member(
            "f", "M2", "H1", "童", "5-11", "child", "2026-01-01")
        self.app.identity.grant_consent("f", "C1", "H1", "air", "v1", "2026-01-01")
        self.app.sites.register_intervention("IV1", "R001", "p1", "2026-03-01")
        self.app.sites.register_site("S1", "R001", "restaurant", "anon")
        self.app.sensors.register_device("DS", site_id="S1")
        self.app.sensors.add_calibration("KS", "DS", "2026-01-01T00:00:00Z", "2026-01-01")
        self.app.sensors.register_device("DH", "household",
                                         analysis_id=mother["analysis_id"])
        self.app.sensors.add_calibration("KH", "DH", "2026-01-01T00:00:00Z",
                                         "2026-01-01", gain=2.0)
        self.app.sensors.ingest_batch("DS", [
            {"window_start": "2026-02-01T00:00:00Z",
             "window_end": "2026-02-01T02:00:00Z", "pm25": 30.0},
            {"window_start": "2026-04-01T00:00:00Z",
             "window_end": "2026-04-01T02:00:00Z", "pm25": 10.0},
            {"window_start": "2026-05-01T00:00:00Z",
             "window_end": "2026-05-01T02:00:00Z", "pm25": 40.0}])
        self.app.sensors.ingest_batch("DH", [
            {"window_start": "2026-02-03T00:00:00Z",
             "window_end": "2026-02-03T02:00:00Z", "pm25": 15.0},
            {"window_start": "2026-04-03T00:00:00Z",
             "window_end": "2026-04-03T02:00:00Z", "pm25": 5.0}])
        self.app.timeline.build("2026-01-01", "2026-06-30")
        return mother, child

    def test_freeze_covers_all_204_regions_and_verifies(self):
        self._seed_r001()
        info = self.app.estimation.create_frozen_estimate(
            "analyst", "E1", "t", "2026-01-01", "2026-06-30")
        self.assertEqual(info["region_count"], REGION_COUNT)
        rows = self.app.estimation.get_region_results("E1")
        self.assertEqual(len(rows), REGION_COUNT)
        self.assertTrue(self.app.estimation.verify_freeze("E1")["ok"])

    def test_weighted_point_matches_hand_calculation(self):
        self._seed_r001()
        self.app.estimation.create_frozen_estimate(
            "analyst", "E1", "t", "2026-01-01", "2026-06-30")
        r001 = next(r for r in self.app.estimation.get_region_results("E1")
                    if r["region_code"] == "R001")
        # 场所读数 30/10/40（餐厅权重 1.3），家庭读数校准后 30/10（妇女权重 1.25）
        self.assertAlmostEqual(r001["point"], 24.0625, places=4)
        self.assertLessEqual(r001["ci_low"], r001["point"])
        self.assertGreaterEqual(r001["ci_high"], r001["point"])

    def test_bootstrap_ci_is_deterministic_with_seed(self):
        self._seed_r001()
        method = {"estimator": "weighted_mean",
                  "bootstrap": {"seed": 7, "n_boot": 200, "ci": 0.95},
                  "min_readings_ok": 3}
        self.app.estimation.create_frozen_estimate(
            "analyst", "E1", "t", "2026-01-01", "2026-06-30", method=method)
        self.app.estimation.create_frozen_estimate(
            "analyst", "E2", "t", "2026-01-01", "2026-06-30", method=method)
        q1 = {r["region_code"]: r for r in self.app.estimation.get_region_results("E1")}
        q2 = {r["region_code"]: r for r in self.app.estimation.get_region_results("E2")}
        self.assertEqual((q1["R001"]["ci_low"], q1["R001"]["ci_high"]),
                         (q2["R001"]["ci_low"], q2["R001"]["ci_high"]))

    def test_frozen_versions_are_immutable(self):
        self._seed_r001()
        self.app.estimation.create_frozen_estimate(
            "analyst", "E1", "t", "2026-01-01", "2026-06-30")
        with self.assertRaises(ImmutableVersionError):
            self.app.estimation.create_frozen_estimate(
                "analyst", "E1", "t2", "2026-01-01", "2026-06-30")
        with self.assertRaises(ImmutableVersionError):
            self.app.estimation.update_version("E1", title="x")

    def test_invalid_weights_rejected(self):
        self._seed_r001()
        with self.assertRaises(ValidationError):
            self.app.estimation.create_frozen_estimate(
                "analyst", "E1", "t", "2026-01-01", "2026-06-30",
                weights={"site_type": {"restaurant": -1.0},
                         "member_role": {"woman": 1.0}})

    def test_calibration_failure_after_freeze_changes_new_version_only(self):
        self._seed_r001()
        self.app.estimation.create_frozen_estimate(
            "analyst", "E1", "t", "2026-01-01", "2026-06-30")
        self.app.sensors.invalidate_calibration("KS")
        self.app.estimation.create_frozen_estimate(
            "analyst", "E2", "t", "2026-01-01", "2026-06-30")
        r1 = next(r for r in self.app.estimation.get_region_results("E1")
                  if r["region_code"] == "R001")
        r2 = next(r for r in self.app.estimation.get_region_results("E2")
                  if r["region_code"] == "R001")
        self.assertEqual(r1["n_readings"], 5)
        self.assertEqual(r2["n_readings"], 2)  # 3 条场所读数被排除，家庭读数保留
        # 旧冻结版本哈希仍可验证（未被改写）
        self.assertTrue(self.app.estimation.verify_freeze("E1")["ok"])


class FeedbackEthicsTest(StudyTestCase):
    def _freeze(self):
        self.app.identity.register_household("f", "H1", "c", "R001", "ref", "2026-01-01")
        mother = self.app.identity.register_member(
            "f", "M1", "H1", "母", "18-59", "woman", "2026-01-01")
        self.app.identity.register_member(
            "f", "M2", "H1", "童", "5-11", "child", "2026-01-01")
        self.app.identity.grant_consent("f", "C1", "H1", "air", "v1", "2026-01-01")
        self.app.sites.register_site("S1", "R001", "restaurant", "anon")
        self.app.sensors.register_device("DS", site_id="S1")
        self.app.sensors.add_calibration("KS", "DS", "2026-01-01T00:00:00Z", "2026-01-01")
        self.app.sensors.ingest_batch("DS", [
            {"window_start": f"2026-{m:02d}-01T00:00:00Z",
             "window_end": f"2026-{m:02d}-01T02:00:00Z", "pm25": 20.0}
            for m in (1, 2, 3)])
        self.app.identity.add_followup("f", "F1", "M2", "2026-02-10",
                                       ["cough", "wheeze"])
        self.app.timeline.build("2026-01-01", "2026-06-30")
        self.app.estimation.create_frozen_estimate(
            "analyst", "E1", "t", "2026-01-01", "2026-06-30")
        return mother

    def test_small_cells_suppressed_in_community_feedback(self):
        self._freeze()
        fb = self.app.feedback.version_feedback("E1")
        r001 = next(c for c in fb["cells"] if c["region_code"] == "R001")
        # 只有 1 个家庭：整区抑制，且不暴露任何暴露数值
        self.assertTrue(r001["suppressed"])
        self.assertNotIn("point", r001)
        self.assertNotIn("ci_low", r001)
        self.assertEqual(r001["n_households_bucket"], "<5")

    def test_released_when_five_households(self):
        mother = self._freeze()
        for i in range(2, 6):
            hh = f"H{i}"
            self.app.identity.register_household(
                "f", hh, "c", "R001", f"ref{i}", "2026-01-01")
            self.app.identity.register_member(
                "f", f"MX{i}", hh, "p", "18-59", "woman", "2026-01-01")
            self.app.identity.grant_consent(
                "f", f"C{i}", hh, "air", "v1", "2026-01-01")
        self.app.timeline.build("2026-01-01", "2026-06-30")
        self.app.estimation.create_frozen_estimate(
            "analyst", "E2", "t", "2026-01-01", "2026-06-30")
        fb = self.app.feedback.version_feedback("E2")
        r001 = next(c for c in fb["cells"] if c["region_code"] == "R001")
        self.assertFalse(r001["suppressed"])
        self.assertIn("point", r001)

    def test_ethics_trace_links_aggregate_to_consent_and_quality(self):
        self._freeze()
        trace = self.app.ethics.region_trace("E1", "R001")
        self.assertEqual(trace["consent"]["persons_total"], 2)
        self.assertEqual(trace["data_quality"]["readings_included"], 3)
        self.assertTrue(all(
            p["consent_currently_active"] for p in trace["persons"]))

    def test_targeted_notification_uses_fixed_template_without_exposure(self):
        self._freeze()
        affected = self.app.ethics.affected_analysis_ids(
            "E1", "R001", symptom_alert=True)
        self.assertEqual(len(affected["analysis_ids"]), 1)  # 仅出现症状的儿童
        note = self.app.identity.create_notification(
            "ethics", "N1", "symptom_followup", affected["analysis_ids"])
        self.assertNotIn("20", note["message"])  # 不泄漏读数/估计值
        self.assertIn("不含监测数值", note["message"])
        with self.assertRaises(ValidationError):
            self.app.identity.create_notification(
                "ethics", "N2", "not_a_template", affected["analysis_ids"])

    def test_consent_lapse_is_traceable_after_freeze(self):
        mother = self._freeze()
        self.app.identity.revoke_consent("f", "C1", "2026-07-01")
        lapsed = self.app.ethics.affected_analysis_ids(
            "E1", "R001", consent_lapsed=True)
        self.assertEqual(len(lapsed["analysis_ids"]), 2)


class AccessControlTest(StudyTestCase):
    def test_role_permissions_separated(self):
        field_key = self.keys["field_coordinator"]
        community_key = self.keys["community_responder"]
        self.assertEqual(self.app.access.authenticate(field_key), "field_coordinator")
        self.assertTrue(self.app.access.authorize("field_coordinator", "consent:write"))
        self.assertFalse(self.app.access.authorize("field_coordinator", "estimate:write"))
        self.assertTrue(self.app.access.authorize("community_responder", "feedback:read"))
        self.assertFalse(self.app.access.authorize("community_responder", "lineage:read"))
        self.assertIsNone(self.app.access.authenticate("not-a-key"))


if __name__ == "__main__":
    unittest.main()
