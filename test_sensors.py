"""设备去重、校准失效保留与缺测统计测试。"""

import unittest

from study.errors import Conflict, ValidationError
from test_support import build_scenario


def reading(day, pm, hour=0):
    return {"sample_start": f"{day}T{hour:02d}:00:00Z",
            "sample_end": f"{day}T{hour:02d}:59:00Z",
            "pm25": pm}


class SensorTest(unittest.TestCase):
    def setUp(self):
        self.ctx = build_scenario(with_readings=False)
        self.app = self.ctx["app"]
        self.field = self.ctx["field"]

    def test_backfill_deduplicates_by_serial_and_window(self):
        batch = [reading("2026-01-10", 30), reading("2026-01-11", 31)]
        first = self.app.sensors.ingest_batch(self.field, "SN-1", batch)
        self.assertEqual((first["accepted"], first["duplicates"]), (2, 0))
        # 离线设备补传同一窗口：跳过，不重复累计。
        again = self.app.sensors.ingest_batch(self.field, "SN-1", batch)
        self.assertEqual((again["accepted"], again["duplicates"]), (0, 2))
        rows = self.app.sensors.query_readings(location_ref=self.ctx["hh1"])
        self.assertEqual(len(rows), 2)

    def test_same_window_different_value_is_conflict(self):
        self.app.sensors.ingest_batch(self.field, "SN-1",
                                      [reading("2026-01-10", 30)])
        with self.assertRaises(Conflict):
            self.app.sensors.ingest_batch(self.field, "SN-1",
                                          [reading("2026-01-10", 99)])

    def test_revoked_calibration_keeps_reading_but_excludes_from_estimates(self):
        # 登记第二个设备用独立校准便于撤销。
        self.app.sensors.ingest_batch(
            self.field, "SN-2", [reading("2026-01-10", 28)])
        calibration_id = [
            c for c in self._calibrations() if c["serial"] == "SN-2"][0][
            "calibration_id"]
        self.app.sensors.revoke_calibration(
            self.field, calibration_id, "2026-01-09", reason="漂移")
        all_rows = self.app.sensors.query_readings()
        self.assertEqual(
            [r["calibration_status"] for r in all_rows
             if r["serial"] == "SN-2"], ["invalid"])
        # 数据仍然保留。
        self.assertEqual(len(self.app.sensors.query_readings()), 1)
        valid = self.app.sensors.query_readings(valid_only=True)
        self.assertEqual(valid, [])

    def _calibrations(self):
        # 校准没有公开列表，经由一次撤销冲突不可达，这里直接读存储。
        return list(self.app.sensors._calibrations.values())

    def test_reading_outside_calibration_window_is_invalid_but_kept(self):
        # SN-1 校准自 2026-01-01；读 2025-12-31 需要更早部署，先补部署。
        self.app.sensors.close_deployment(self.field, "SN-1", "2026-01-05")
        self.app.sensors.deploy(self.field, "SN-1", "household",
                                self.ctx["hh1"], "2025-12-25",
                                end_date="2026-01-01",
                                interval_minutes=1440)
        # 该日无校准 → 读数保留但 invalid。
        self.app.sensors.ingest_batch(
            self.field, "SN-1",
            [{"sample_start": "2025-12-30T00:00:00Z",
              "sample_end": "2025-12-30T23:59:00Z", "pm25": 50}])
        rows = self.app.sensors.query_readings()
        self.assertEqual(rows[0]["calibration_status"], "invalid")

    def test_reading_without_deployment_rejected(self):
        # 先闭合开放部署，2030 的读数才落在任何部署之外。
        self.app.sensors.close_deployment(self.field, "SN-1", "2026-02-01")
        with self.assertRaises(ValidationError):
            self.app.sensors.ingest_batch(
                self.field, "SN-1",
                [{"sample_start": "2030-01-01T00:00:00Z",
                  "sample_end": "2030-01-01T23:59:00Z", "pm25": 10}])

    def test_overlapping_deployment_and_calibration_rejected(self):
        with self.assertRaises(Conflict):
            self.app.sensors.deploy(self.field, "SN-1", "household",
                                    self.ctx["hh1"], "2026-02-01")
        with self.assertRaises(Conflict):
            self.app.sensors.add_calibration(self.field, "SN-1",
                                             "2026-02-01")

    def test_quality_counts_separate_valid_invalid_missing(self):
        self.app.sensors.ingest_batch(
            self.field, "SN-1", [reading("2026-01-10", 30)])
        self.app.sensors.close_deployment(self.field, "SN-1", "2026-01-11")
        counts = self.app.sensors.quality_counts(self.ctx["hh1"])
        self.assertEqual(counts["valid_readings"], 1)
        self.assertEqual(counts["invalid_readings"], 0)
        # 部署 10 天（01-01 至 01-11），日采样应有 10 个窗口。
        self.assertEqual(counts["missing_windows"], 9)

    def test_missing_slots_enumeration(self):
        from study.util import parse_date
        slots = self.app.sensors.missing_slots("SN-1", parse_date("2026-01-10"))
        # 日采样设备该日仅一个窗口且未上报 → 1 个缺测。
        self.assertEqual(len(slots), 1)


if __name__ == "__main__":
    unittest.main()
