"""身份分库、同意、搬家与退出的领域测试。"""

import unittest

from study.app import StudyApp
from study.errors import Conflict, PermissionDenied, ValidationError


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.app = StudyApp()
        self.app.catalog.add_community("C001", "R001")
        self.app.catalog.add_community("C002", "R002")

    def test_analysis_roster_has_no_identity_fields(self):
        hh = self.app.vault.create_household(
            "field", "C001", contact_name="张三", contact_phone="110",
            address="某村1号")["household_id"]
        self.app.vault.add_member("field", hh, "2022-06-01",
                                  name="张小孩")
        roster = self.app.vault.analysis_roster("analyst")
        self.assertEqual(len(roster), 1)
        row = roster[0]
        self.assertNotIn("name", row)
        self.assertNotIn("contact_name", row)
        self.assertNotIn("address", row)
        self.assertNotIn("birth_date", row)
        self.assertEqual(row["age_band"], "under_5")
        self.assertTrue(row["analysis_id"].startswith("A-"))

    def test_analyst_cannot_read_identity_or_resolve(self):
        hh = self.app.vault.create_household(
            "field", "C001", contact_name="张三")["household_id"]
        aid = self.app.vault.add_member("field", hh, "2022-01-01")[
            "analysis_id"]
        with self.assertRaises(PermissionDenied):
            self.app.vault.get_household_identity("analyst", hh)
        with self.assertRaises(PermissionDenied):
            self.app.vault.resolve_analysis_id("analyst", aid)

    def test_consent_intervals_cannot_overlap_and_end_truncates(self):
        hh = self.app.vault.create_household("field", "C001")[
            "household_id"]
        self.app.vault.grant_consent("field", hh, "2026-01-01")
        with self.assertRaises(Conflict):
            self.app.vault.grant_consent("field", hh, "2026-02-01",
                                         "2026-03-01")
        self.app.vault.end_consent("field", hh, "2026-02-01",
                                   reason="撤回")
        self.app.vault.grant_consent("field", hh, "2026-03-01")
        intervals = self.app.vault.consent_intervals(hh)
        self.assertEqual(intervals[0]["end"], "2026-02-01")
        self.assertIsNone(intervals[1]["end"])

    def test_move_truncates_residency_by_real_date(self):
        hh1 = self.app.vault.create_household("field", "C001")[
            "household_id"]
        hh2 = self.app.vault.create_household("field", "C002")[
            "household_id"]
        aid = self.app.vault.add_member(
            "field", hh1, "2000-01-01", move_in="2026-01-01")[
            "analysis_id"]
        self.app.vault.move_member("field", aid, hh2, "2026-03-01")
        episodes = self.app.vault.residency_episodes(aid)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0]["end"], "2026-03-01")
        self.assertEqual(episodes[1]["community_id"], "C002")
        self.assertIsNone(episodes[1]["end"])
        # 不能往回搬在开始日之前。
        with self.assertRaises(ValidationError):
            self.app.vault.move_member("field", aid, hh1, "2026-02-01")

    def test_withdrawal_and_pseudo_resolution_audited(self):
        hh = self.app.vault.create_household("field", "C001")[
            "household_id"]
        aid = self.app.vault.add_member("field", hh, "2022-01-01")[
            "analysis_id"]
        self.app.vault.withdraw_member("field", aid, "2026-04-01",
                                       reason="退出")
        self.assertEqual(self.app.vault.withdrawal(aid)["date"],
                         "2026-04-01")
        self.app.vault.resolve_analysis_id("ethics", aid)
        actions = [e["action"] for e in self.app.audit.entries("ethics")]
        self.assertIn("member_withdrawn", actions)
        self.assertIn("pseudo_resolved", actions)


if __name__ == "__main__":
    unittest.main()
