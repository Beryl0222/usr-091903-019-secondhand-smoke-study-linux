"""HTTP 层端到端契约测试：角色头、路由、错误码与完整研究链路。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from study.app import StudyApp
from study.server import health_payload, make_handler


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = StudyApp()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         make_handler(cls.app))
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, payload=None, role=None, query=None):
        url = self.base + path
        if query:
            url += "?" + query
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if role:
            headers["X-Actor-Role"] = role
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_health_identity_unchanged(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_requires_role_header(self):
        status, body = self.call("GET", "/roster")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")

    def test_full_study_flow_over_http(self):
        # 目录
        status, body = self.call("GET", "/regions")
        self.assertEqual(status, 200)
        self.assertEqual(body["region_total"], 204)
        status, _ = self.call("POST", "/communities",
                              {"community_id": "C001",
                               "region_code": "R001"}, role="field")
        self.assertEqual(status, 201)
        self.call("POST", "/interventions",
                  {"version": "v1", "effective_date": "2026-03-01"},
                  role="field")
        self.call("POST", "/interventions/assign",
                  {"community_id": "C001", "version": "v1"}, role="field")
        status, venue = self.call("POST", "/venues",
                                  {"venue_type": "restaurant",
                                   "community_id": "C001"}, role="field")
        self.assertEqual(status, 201)

        # 家庭/成员/同意
        status, hh = self.call("POST", "/households",
                               {"community_id": "C001",
                                "contact_name": "甲",
                                "contact_phone": "1001"}, role="field")
        self.assertEqual(status, 201)
        hid = hh["household_id"]
        status, member = self.call("POST", "/members",
                                   {"household_id": hid,
                                    "birth_date": "2023-01-01"},
                                   role="field")
        aid = member["analysis_id"]
        self.assertTrue(aid.startswith("A-"))
        self.call("POST", "/consents/grant",
                  {"household_id": hid, "start_date": "2026-01-01"},
                  role="field")

        # 设备/校准/部署/读数
        self.call("POST", "/devices", {"serial": "SN-1"}, role="field")
        self.call("POST", "/calibrations",
                  {"serial": "SN-1", "valid_from": "2026-01-01"},
                  role="field")
        self.call("POST", "/deployments",
                  {"serial": "SN-1", "location_kind": "household",
                   "location_ref": hid, "start_date": "2026-01-01",
                   "interval_minutes": 1440}, role="field")
        days = [f"2026-01-{d:02d}" for d in range(10, 15)] + \
               [f"2026-04-{d:02d}" for d in range(10, 15)]
        status, batch = self.call("POST", "/readings/batch", {
            "serial": "SN-1",
            "readings": [{"sample_start": f"{d}T00:00:00Z",
                          "sample_end": f"{d}T23:59:00Z",
                          "pm25": 35 if d < "2026-03" else 15}
                         for d in days]}, role="field")
        self.assertEqual(status, 200)
        self.assertEqual(batch["accepted"], 10)
        # 重复补传
        status, batch = self.call("POST", "/readings/batch", {
            "serial": "SN-1",
            "readings": [{"sample_start": f"{d}T00:00:00Z",
                          "sample_end": f"{d}T23:59:00Z",
                          "pm25": 35 if d < "2026-03" else 15}
                         for d in days]}, role="field")
        self.assertEqual(batch["duplicates"], 10)

        # 校准失效读数保留但排除
        status, cal = self.call("POST", "/calibrations",
                                {"serial": "SN-1",
                                 "valid_from": "2027-01-01",
                                 "valid_to": "2028-01-01"}, role="field")
        # 物化前闭合部署
        self.call("POST", "/deployments/close",
                  {"serial": "SN-1", "date": "2026-05-01"}, role="field")
        self.call("POST", "/exposure/materialize",
                  {"window_start": "2026-01-01",
                   "window_end": "2026-05-01"}, role="analyst")

        # 症状
        status, _ = self.call("POST", "/symptoms",
                              {"analysis_id": aid,
                               "symptom_date": "2026-04-12",
                               "symptoms": ["cough"]}, role="field")
        self.assertEqual(status, 201)

        # 估算/冻结
        weights = {"under_5": 1.0, "5_17": 0.0, "18_59": 0.0,
                   "60_plus": 0.0}
        status, run = self.call("POST", "/estimates/run",
                                {"window_start": "2026-01-01",
                                 "window_end": "2026-05-01",
                                 "weights": weights}, role="analyst")
        self.assertEqual(status, 201)
        status, freeze = self.call("POST", "/estimates/freeze",
                                   {"run_id": run["run_id"]},
                                   role="analyst")
        self.assertEqual(status, 201)
        fz_id = freeze["freeze_id"]
        status, verify = self.call("POST", f"/freezes/{fz_id}/verify",
                                   {}, role="ethics")
        self.assertEqual(status, 200)
        self.assertTrue(verify["intact"])

        # 社区反馈
        status, report = self.call(
            "GET", f"/communities/C001/report",
            query=f"freeze_id={fz_id}", role="community")
        self.assertEqual(status, 200)
        self.assertNotIn(hid, json.dumps(report, ensure_ascii=False))

        # 伦理聚合与溯源
        status, overview = self.call(
            "GET", f"/ethics/freeze/{fz_id}/overview", role="ethics")
        self.assertEqual(status, 200)
        self.assertEqual(overview["region_total"], 204)
        status, traced = self.call(
            "GET", f"/ethics/freeze/{fz_id}/regions/R001", role="ethics")
        self.assertEqual(status, 200)
        self.assertIn(aid, json.dumps(traced))

        # 定向通知
        status, notice = self.call("POST", "/notifications", {
            "analysis_ids": [aid], "reason": "症状随访",
            "advice": "建议到社区诊所咨询"}, role="ethics")
        self.assertEqual(status, 201)
        nid = notice["notification_id"]
        status, _ = self.call("POST", f"/notifications/{nid}/deliver",
                              {"household_id": hid,
                               "outcome": "delivered"}, role="field")
        self.assertEqual(status, 200)

    def test_analyst_forbidden_from_notification_and_identity(self):
        status, _ = self.call("POST", "/notifications",
                              {"analysis_ids": ["A-000001"],
                               "reason": "x", "advice": "y"},
                              role="analyst")
        self.assertEqual(status, 403)

    def test_unknown_route_404(self):
        status, _ = self.call("GET", "/nope", role="field")
        self.assertEqual(status, 404)

    def test_validation_error_has_stable_code(self):
        status, body = self.call("POST", "/households",
                                 {"community_id": "R404"}, role="field")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
