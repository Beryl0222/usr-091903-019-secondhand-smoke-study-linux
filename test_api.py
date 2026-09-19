"""端到端 HTTP 契约测试：鉴权、角色隔离与完整研究流程走网络栈。"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, health_payload
from study.api import make_handler


def _build_test_handler(dirpath):
    def factory():
        from study.app import StudyApp
        return StudyApp(os.path.join(dirpath, "identity.db"),
                        os.path.join(dirpath, "analysis.db"))
    return make_handler(factory)


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="shs-api-")
        cls.handler_cls = _build_test_handler(cls.dir)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler_cls)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        # 初始化应用与演示密钥
        from study.app import StudyApp
        cls.app = StudyApp(os.path.join(cls.dir, "identity.db"),
                           os.path.join(cls.dir, "analysis.db"))
        cls.server.study_app = cls.app
        cls.keys = cls.app.init_demo_keys()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.app.close()

    def call(self, method, path, key=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-API-Key"] = key
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    # ---- 基础契约 ------------------------------------------------------
    def test_health_anonymous_and_identity_stable(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok", "service": SERVICE_ID,
                                   "name": "二手烟暴露干预研究"})
        self.assertEqual(health_payload()["service"], SERVICE_ID)

    def test_unknown_route_404(self):
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)

    def test_domain_route_requires_key(self):
        status, payload = self.call("GET", "/estimates")
        self.assertEqual(status, 401)
        self.assertIn("密钥", payload["error"])

    def test_role_cannot_cross_permissions(self):
        # 社区反馈员不能登记家庭
        status, _ = self.call("POST", "/households",
                              key=self.keys["community_responder"],
                              body={"household_id": "X"})
        self.assertEqual(status, 403)
        # 现场协调员不能冻结估算
        status, _ = self.call("POST", "/estimates",
                              key=self.keys["field_coordinator"],
                              body={"version_id": "X"})
        self.assertEqual(status, 403)
        # 分析人员不能读社区反馈（反馈只面向社区反馈员）
        self.call("POST", "/sites", key=self.keys["analyst"],
                  body={"site_id": "S1", "region_code": "R001",
                        "site_type": "restaurant", "anonymized_label": "a"})
        status, _ = self.call("GET", "/feedback/E1",
                              key=self.keys["analyst"])
        self.assertEqual(status, 403)

    # ---- 完整流程 ------------------------------------------------------
    def test_full_workflow_over_http(self):
        field, analyst = self.keys["field_coordinator"], self.keys["analyst"]
        community, ethics = (self.keys["community_responder"],
                             self.keys["ethics_officer"])

        status, hh = self.call("POST", "/households", key=field, body={
            "household_id": "HH1", "community": "河边", "region_code": "R001",
            "contact_ref": "ref-1", "enrolled_on": "2026-01-01"})
        self.assertEqual(status, 201)
        status, mother = self.call("POST", "/members", key=field, body={
            "member_id": "M1", "household_id": "HH1", "pseudonym": "母",
            "age_band": "18-59", "role": "woman", "enrolled_on": "2026-01-01"})
        self.assertEqual(status, 201)
        status, child = self.call("POST", "/members", key=field, body={
            "member_id": "M2", "household_id": "HH1", "pseudonym": "童",
            "age_band": "5-11", "role": "child", "enrolled_on": "2026-01-01"})
        self.assertEqual(status, 201)
        self.assertNotEqual(mother["analysis_id"], child["analysis_id"])
        self.call("POST", "/consents", key=field, body={
            "consent_id": "C1", "household_id": "HH1", "scope": "air",
            "version": "v1", "granted_on": "2026-01-01"})

        self.call("POST", "/interventions", key=analyst, body={
            "intervention_id": "IV1", "region_code": "R001",
            "policy_version": "p1", "effective_date": "2026-03-01"})
        self.call("POST", "/sites", key=analyst, body={
            "site_id": "S2", "region_code": "R001", "site_type": "restaurant",
            "anonymized_label": "anon"})
        self.call("POST", "/devices", key=analyst, body={
            "device_serial": "D1", "site_id": "S2"})
        self.call("POST", "/calibrations", key=analyst, body={
            "session_id": "K1", "device_serial": "D1",
            "calibrated_at": "2026-01-01T00:00:00Z", "valid_from": "2026-01-01"})

        samples = [{"window_start": f"2026-{m:02d}-01T00:00:00Z",
                    "window_end": f"2026-{m:02d}-01T02:00:00Z", "pm25": 20.0}
                   for m in (1, 2, 3, 4, 5)]
        status, ingested = self.call("POST", "/readings:bulk", key=analyst, body={
            "device_serial": "D1", "samples": samples})
        self.assertEqual(status, 200)
        self.assertEqual(ingested["accepted"], 5)
        # 离线补传整批重发：全部判重，原值保留
        status, again = self.call("POST", "/readings:bulk", key=analyst, body={
            "device_serial": "D1", "samples": samples})
        self.assertEqual(again["duplicates"], 5)

        status, built = self.call("POST", "/timeline/build", key=analyst, body={
            "period_start": "2026-01-01", "period_end": "2026-06-30"})
        self.assertEqual(built["n_periods"], 4)  # 2 成员 × 政策前后两段

        status, frozen = self.call("POST", "/estimates", key=analyst, body={
            "version_id": "E1", "title": "上半年",
            "period_start": "2026-01-01", "period_end": "2026-06-30"})
        self.assertEqual(status, 201)
        self.assertEqual(frozen["region_count"], 204)

        status, verify = self.call("GET", "/estimates/E1/verify", key=analyst)
        self.assertEqual(status, 200)
        self.assertTrue(verify["ok"])

        # 社区只看到抑制后的反馈
        status, fb = self.call("GET", "/feedback/E1", key=community)
        self.assertEqual(status, 200)
        r001 = next(c for c in fb["cells"] if c["region_code"] == "R001")
        self.assertTrue(r001["suppressed"])
        self.assertNotIn("point", r001)

        # 伦理可追溯并发起定向通知
        status, trace = self.call(
            "GET", "/ethics/versions/E1/regions/R001/trace", key=ethics)
        self.assertEqual(status, 200)
        self.assertEqual(trace["consent"]["persons_total"], 2)
        self.assertEqual(trace["data_quality"]["readings_included"], 5)

        self.call("POST", "/followups", key=field, body={
            "followup_id": "F1", "member_id": "M2", "observed_on": "2026-04-10",
            "symptoms": ["cough", "wheeze"]})
        # 冻结后出现的随访不改变已冻结血缘
        status, affected = self.call(
            "GET", "/ethics/versions/E1/regions/R001/affected"
                  "?symptom_alert=true", key=ethics)
        self.assertEqual(affected["count"], 0)

        status, risk = self.call("POST", "/risks", key=ethics, body={
            "risk_id": "RK1", "reason": "社区走访发现风险", "severity": "medium",
            "version_id": "E1", "region_code": "R001"})
        self.assertEqual(status, 201)
        status, note = self.call("POST", "/notifications", key=ethics, body={
            "notification_id": "N1", "reason_code": "participation",
            "analysis_ids": [mother["analysis_id"]], "risk_id": "RK1"})
        self.assertEqual(status, 201)
        self.assertNotIn("20", note["message"])
        status, _ = self.call("POST", "/notifications/N1/deliver", key=field,
                              body={"delivery_note": "电话联系"})
        self.assertEqual(status, 200)
        # 再建一条未送达通知，对比两种视图的可见信息
        self.call("POST", "/notifications", key=ethics, body={
            "notification_id": "N2", "reason_code": "data_quality",
            "analysis_ids": [mother["analysis_id"]]})
        # 现场协调员送达前可取到联系方式
        status, field_view = self.call("GET", "/notifications/N2", key=field)
        self.assertEqual(status, 200)
        self.assertEqual(field_view["targets"][0]["contact_ref"], "ref-1")
        # 伦理角色无权取联系方式视图
        status, _ = self.call("GET", "/notifications/N2", key=ethics)
        self.assertEqual(status, 403)
        # 伦理列表只含分析编号，不含联系方式/代称
        status, ethics_view = self.call("GET", "/notifications", key=ethics)
        self.assertEqual(status, 200)
        serialized = json.dumps(ethics_view, ensure_ascii=False)
        self.assertNotIn("ref-1", serialized)
        self.assertNotIn("contact_ref", serialized)
        status, listing = self.call(
            "GET", "/notifications?undelivered_only=true", key=ethics)
        self.assertEqual(len(listing["notifications"]), 1)
        self.assertEqual(listing["notifications"][0]["notification_id"], "N2")


if __name__ == "__main__":
    unittest.main()
