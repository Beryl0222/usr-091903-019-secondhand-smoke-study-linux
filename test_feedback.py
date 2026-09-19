"""社区反馈抑制、伦理聚合溯源与定向通知测试。"""

import unittest

from study.app import StudyApp
from study.errors import PermissionDenied, ValidationError

WEIGHTS = {"under_5": 0.5, "5_17": 0.0, "18_59": 0.5, "60_plus": 0.0}


def build_multi_household(households=3):
    """3 户同社区、均有幼儿的场景，使年龄段格子越过抑制阈值。"""
    app = StudyApp()
    field, analyst, ethics = "field", "analyst", "ethics"
    app.catalog.add_community("C001", "R001")
    app.catalog.register_intervention("v1", "2026-03-01")
    app.catalog.assign_intervention("C001", "v1")
    venue = app.catalog.add_venue("restaurant", "C001")
    hh_ids, aids = [], []
    for i in range(households):
        hid = app.vault.create_household(
            field, "C001", contact_name=f"户{i}",
            contact_phone=f"100{i}")["household_id"]
        app.vault.add_member(field, hid, "2023-01-01")
        app.vault.grant_consent(field, hid, "2026-01-01")
        serial = f"SN-{i}"
        app.sensors.register_device(field, serial)
        app.sensors.add_calibration(field, serial, "2026-01-01")
        app.sensors.deploy(field, serial, "household", hid,
                           "2026-01-01", interval_minutes=1440)
        pre = [f"2026-01-{d:02d}" for d in range(10, 15)]
        post = [f"2026-04-{d:02d}" for d in range(10, 15)]
        app.sensors.ingest_batch(field, serial, [{
            "sample_start": f"{day}T00:00:00Z",
            "sample_end": f"{day}T23:59:00Z",
            "pm25": 35 if day < "2026-03-01" else 15,
        } for day in pre + post])
        hh_ids.append(hid)
    app.sensors.register_device(field, "SN-V")
    app.sensors.add_calibration(field, "SN-V", "2026-01-01")
    app.sensors.deploy(field, "SN-V", "venue", venue["venue_id"],
                       "2026-01-01", interval_minutes=1440)
    days = [f"2026-01-{d:02d}" for d in range(10, 15)] + \
           [f"2026-04-{d:02d}" for d in range(10, 15)]
    app.sensors.ingest_batch(field, "SN-V", [{
        "sample_start": f"{day}T00:00:00Z",
        "sample_end": f"{day}T23:59:00Z", "pm25": 30} for day in days])
    for serial in ["SN-V"] + [f"SN-{i}" for i in range(households)]:
        app.sensors.close_deployment(field, serial, "2026-05-01")
    app.exposure.materialize(analyst, "2026-01-01", "2026-05-01")
    run = app.estimates.run(analyst, "2026-01-01", "2026-05-01", WEIGHTS)
    freeze_id = app.estimates.freeze(analyst, run["run_id"])["freeze_id"]
    return app, freeze_id, hh_ids


class CommunityFeedbackTest(unittest.TestCase):
    def test_report_shown_when_above_thresholds(self):
        app, freeze_id, hh_ids = build_multi_household(3)
        report = app.feedback.community_report(
            "community", "C001", freeze_id)
        pre_bands = {c["age_band"]: c for c in report["household_cells"][0][
            "age_bands"]}
        self.assertFalse(pre_bands["under_5"]["suppressed"])
        self.assertEqual(pre_bands["under_5"]["mean_pm25"], 35.0)
        # 没有任何家庭标识：报告文本与结构中不出现家庭编号/联系方式。
        blob = str(report)
        for hid in hh_ids:
            self.assertNotIn(hid, blob)
        self.assertNotIn("1000", blob)

    def test_report_suppresses_small_cells(self):
        app, freeze_id, _hh = build_multi_household(households=1)
        report = app.feedback.community_report(
            "community", "C001", freeze_id)
        cell = report["household_cells"][0]["age_bands"][0]
        self.assertTrue(cell["suppressed"])
        self.assertEqual(cell["reason"], "small_household_count")

    def test_venue_feedback_uses_type_only(self):
        app, freeze_id, _hh = build_multi_household(3)
        report = app.feedback.community_report(
            "community", "C001", freeze_id)
        venue_summary = report["venue_type_summary"][0]
        self.assertNotIn("venue_id", venue_summary)
        self.assertEqual(venue_summary["venue_type"], "restaurant")
        self.assertFalse(venue_summary["suppressed"])

    def test_community_role_cannot_open_identity(self):
        app, freeze_id, _hh = build_multi_household(1)
        with self.assertRaises(PermissionDenied):
            app.vault.get_household_identity("community", _hh[0])


class EthicsTraceTest(unittest.TestCase):
    def setUp(self):
        self.app, self.freeze_id, self.hh_ids = build_multi_household(3)

    def test_aggregate_covers_all_regions_with_quality_and_no_values(self):
        view = self.app.ethics.aggregate_view("ethics", self.freeze_id)
        self.assertEqual(view["region_total"], 204)
        r001 = next(r for r in view["regions"]
                    if r["region_code"] == "R001")
        self.assertGreater(r001["consented_person_days"]["pre"], 0)
        self.assertGreater(r001["missing_windows"], 0)
        self.assertNotIn("pm25", str(r001).lower())

    def test_trace_region_finds_quality_issues_and_analysis_ids(self):
        traced = self.app.ethics.trace_region(
            "ethics", self.freeze_id, "R001")
        self.assertTrue(traced["quality_issues"])
        self.assertEqual(len(traced["affected_analysis_ids"]), 3)
        # 下钻结果不含身份明文。
        blob = str(traced)
        self.assertNotIn("户0", blob)
        self.assertNotIn("1000", blob)

    def test_trace_analysis_shows_consent_and_truncation_without_pm(self):
        aid = self.app.vault.analysis_roster("ethics")[0]["analysis_id"]
        detail = self.app.ethics.trace_analysis("ethics", aid)
        self.assertTrue(detail["consent_intervals"])
        self.assertGreater(detail["person_days"], 0)
        self.assertNotIn("pm25", str(detail).lower())

    def test_open_identity_requires_reason_and_is_audited(self):
        aid = self.app.vault.analysis_roster("ethics")[0]["analysis_id"]
        with self.assertRaises(ValidationError):
            self.app.ethics.open_identity("ethics", aid, reason="")
        opened = self.app.ethics.open_identity(
            "ethics", aid, reason="随访发现持续咳喘")
        self.assertIn("contact_phone", opened)
        actions = [e["action"] for e in
                   self.app.audit.entries("ethics")]
        self.assertIn("identity_opened_for_risk", actions)

    def test_analyst_cannot_use_ethics_trace(self):
        with self.assertRaises(PermissionDenied):
            self.app.ethics.aggregate_view("analyst", self.freeze_id)


class NotificationTest(unittest.TestCase):
    def setUp(self):
        self.app, self.freeze_id, self.hh_ids = build_multi_household(3)
        roster = self.app.vault.analysis_roster("ethics")
        self.aids = [r["analysis_id"] for r in roster]

    def test_targeted_notification_dedups_to_households_without_exposure(self):
        notice = self.app.notifications.issue(
            "ethics", self.aids[:2], reason="高风险症状随访",
            advice="建议开窗通风并到社区诊所咨询")
        self.assertEqual(notice["target_count"], 2)
        blob = str(notice)
        # 通知载荷绝不包含暴露数值或暴露排名。
        self.assertNotIn("pm25", blob.lower())
        self.assertNotIn("35", blob)

    def test_same_household_members_collapse_to_one_target(self):
        # 两名成员同属一户时只通知一次（这里直接用同家庭两个成员构造）。
        app = self.app
        app.vault.add_member("field", self.hh_ids[0], "2024-01-01")
        roster = app.vault.analysis_roster("ethics")
        same_home = [r["analysis_id"] for r in roster][:2]
        # 默认场景每户一人；追加成员后取该户两名成员。
        aids_hh0 = [
            aid for aid in [r["analysis_id"] for r in roster]
            if app.vault.household_of_analysis(aid)[0] == self.hh_ids[0]]
        self.assertEqual(len(aids_hh0), 2)
        notice = app.notifications.issue(
            "ethics", aids_hh0, reason="家庭随访", advice="建议就医咨询")
        self.assertEqual(notice["target_count"], 1)

    def test_field_can_deliver_but_not_issue(self):
        with self.assertRaises(PermissionDenied):
            self.app.notifications.issue(
                "field", self.aids[:1], reason="x", advice="y")
        notice = self.app.notifications.issue(
            "ethics", self.aids[:1], reason="高风险", advice="建议咨询")
        hid = self.hh_ids[0]
        updated = self.app.notifications.mark_delivered(
            "field", notice["notification_id"], hid)
        self.assertEqual(updated["deliveries"][hid], "delivered")

    def test_field_list_has_no_analysis_ids(self):
        self.app.notifications.issue(
            "ethics", self.aids[:2], reason="高风险", advice="建议咨询")
        listing = self.app.notifications.list_for_field(
            "field", community_id="C001")
        blob = str(listing)
        for aid in self.aids:
            self.assertNotIn(aid, blob)


if __name__ == "__main__":
    unittest.main()
