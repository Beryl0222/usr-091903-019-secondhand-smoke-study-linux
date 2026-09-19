"""哈希链审计日志测试。"""

import unittest

from study.audit import AuditLog


class AuditChainTest(unittest.TestCase):
    def test_chain_verifies_when_intact(self):
        log = AuditLog()
        log.append("field", "household_created", household_id="HH-0001")
        log.append("ethics", "pseudo_resolved", analysis_id="A-000001")
        ok, broken_at = log.verify()
        self.assertTrue(ok)
        self.assertIsNone(broken_at)
        entries = log.entries("ethics")
        self.assertEqual(len(entries), 2)
        # 对外视图不含哈希字段。
        self.assertNotIn("id", entries[0])
        self.assertNotIn("sealed", entries[0])

    def test_tampering_is_detected(self):
        log = AuditLog()
        log.append("field", "consent_granted", household_id="HH-0001")
        log.append("field", "consent_ended", household_id="HH-0001")
        # 直接删改记录内容应被校验发现。
        log._records[0]["detail"]["household_id"] = "HH-0999"
        ok, broken_at = log.verify()
        self.assertFalse(ok)
        self.assertEqual(broken_at, 1)

    def test_filter_by_action(self):
        log = AuditLog()
        log.append("field", "consent_granted")
        log.append("ethics", "pseudo_resolved")
        log.append("ethics", "pseudo_resolved")
        self.assertEqual(
            len(log.entries("ethics", action="pseudo_resolved")), 2)


if __name__ == "__main__":
    unittest.main()
