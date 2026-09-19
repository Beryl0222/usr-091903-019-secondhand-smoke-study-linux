"""HTTP 路由与最小权限鉴权（仅依赖标准库）。

所有领域接口要求 X-API-Key；/health 保持匿名。应用实例懒加载：
只有命中领域路由时才调用 app_factory()，健康检查与 404 不落库。
"""

import json
import re
from http.server import BaseHTTPRequestHandler

from .errors import StudyError


def make_handler(app_factory):
    """app_factory: 零参可调用对象，首次命中领域路由时返回 StudyApp。"""

    class StudyHandler(BaseHTTPRequestHandler):
        server_version = "SHSStudy/1.0"

        # ---- 通用工具 --------------------------------------------------
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise StudyError(f"请求体不是合法 JSON: {exc}", status=400)
            if not isinstance(data, dict):
                raise StudyError("请求体必须是 JSON 对象", status=400)
            return data

        def log_message(self, *_args):
            return

        # ---- 分发 ------------------------------------------------------
        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            try:
                raw_path, _, query = self.path.partition("?")
                q = _parse_query(query)
                if method == "GET" and raw_path == "/health":
                    self._send(200, health_payload())
                    return
                routes = GET_ROUTES if method == "GET" else POST_ROUTES
                for pattern, permission, handler_name in routes:
                    m = re.fullmatch(pattern, raw_path)
                    if m:
                        # 命中领域路由才创建应用
                        self.app = self.server.study_app if hasattr(self.server, "study_app") \
                            else app_factory()
                        self.server.study_app = self.app
                        self._authorize(permission)
                        if method == "GET":
                            result, status = getattr(self, handler_name)(m, q)
                        else:
                            data = self._read_json()
                            result, status = getattr(self, handler_name)(m, data)
                        self._send(status, result)
                        return
                self._send(404, {"error": f"未知路由: {raw_path}"})
            except StudyError as exc:
                self._send(exc.status, {"error": str(exc)})
            except KeyError as exc:
                self._send(400, {"error": f"缺少必填字段: {exc.args[0]}"})
            except Exception as exc:  # 防御：不把堆栈泄露给调用方
                self._send(500, {"error": f"内部错误: {type(exc).__name__}: {exc}"})

        # ---- GET 处理 --------------------------------------------------
        def h_list_versions(self, _m, _q):
            return {"versions": self.app.estimation.list_versions()}, 200

        def h_get_version(self, m, _q):
            return self.app.estimation.get_version(m.group(1)), 200

        def h_region_results(self, m, q):
            return self.app.estimation.get_region_results(
                m.group(1), q.get("quality")), 200

        def h_verify(self, m, _q):
            return self.app.estimation.verify_freeze(m.group(1)), 200

        def h_feedback(self, m, _q):
            return self.app.feedback.version_feedback(m.group(1)), 200

        def h_trace(self, m, _q):
            return self.app.ethics.region_trace(m.group(1), m.group(2)), 200

        def h_affected(self, m, q):
            return self.app.ethics.affected_analysis_ids(
                m.group(1), m.group(2),
                symptom_alert=_as_bool(q.get("symptom_alert")),
                consent_lapsed=_as_bool(q.get("consent_lapsed"))), 200

        def h_list_notifications(self, m, q):
            return {"notifications": self.app.identity.list_notifications(
                include_undelivered_only=q.get("undelivered_only") == "true")}, 200

        def h_notification_delivery(self, m, _q):
            # 仅现场协调员（notification:deliver）可取联系方式
            return self.app.identity.get_notification_for_delivery(m.group(1)), 200

        # ---- POST 处理 -------------------------------------------------
        def p_household(self, _m, d):
            return self.app.identity.register_household(
                self._role, d["household_id"], d["community"], d["region_code"],
                d["contact_ref"], d["enrolled_on"]), 201

        def p_move(self, m, d):
            return self.app.identity.record_move(
                self._role, m.group(1), d["new_region_code"], d["effective_date"]), 200

        def p_member(self, _m, d):
            return self.app.identity.register_member(
                self._role, d["member_id"], d["household_id"], d["pseudonym"],
                d["age_band"], d["role"], d["enrolled_on"], d.get("analysis_id")), 201

        def p_withdraw(self, m, d):
            return self.app.identity.withdraw_member(
                self._role, m.group(1), d["withdrawn_on"]), 200

        def p_consent(self, _m, d):
            return self.app.identity.grant_consent(
                self._role, d["consent_id"], d["household_id"], d["scope"],
                d["version"], d["granted_on"]), 201

        def p_consent_revoke(self, m, d):
            return self.app.identity.revoke_consent(
                self._role, m.group(1), d["revoked_on"]), 200

        def p_followup(self, _m, d):
            return self.app.identity.add_followup(
                self._role, d["followup_id"], d["member_id"], d["observed_on"],
                d["symptoms"], d.get("note", "")), 201

        def p_site(self, _m, d):
            return self.app.sites.register_site(
                d["site_id"], d["region_code"], d["site_type"],
                d["anonymized_label"]), 201

        def p_intervention(self, _m, d):
            return self.app.sites.register_intervention(
                d["intervention_id"], d["region_code"], d["policy_version"],
                d["effective_date"], d.get("site_id"), d.get("description", "")), 201

        def p_device(self, _m, d):
            return self.app.sensors.register_device(
                d["device_serial"], d.get("assign_type", "site"),
                d.get("site_id"), d.get("analysis_id"), d.get("deployed_on")), 201

        def p_calibration(self, _m, d):
            return self.app.sensors.add_calibration(
                d["session_id"], d["device_serial"], d["calibrated_at"],
                d["valid_from"], d.get("valid_to"), d.get("gain", 1.0),
                d.get("offset", 0.0), d.get("status", "valid")), 201

        def p_calibration_invalidate(self, m, _d):
            return self.app.sensors.invalidate_calibration(m.group(1)), 200

        def p_ingest(self, _m, d):
            return self.app.sensors.ingest_batch(
                d["device_serial"], d["samples"], d.get("ingest_batch")), 200

        def p_timeline_build(self, _m, d):
            return self.app.timeline.build(d["period_start"], d["period_end"]), 200

        def p_estimate(self, _m, d):
            return self.app.estimation.create_frozen_estimate(
                self._role, d["version_id"], d["title"], d["period_start"],
                d["period_end"], d.get("weights"), d.get("method")), 201

        def p_risk(self, _m, d):
            return self.app.identity.register_risk(
                self._role, d["risk_id"], d["reason"], d["severity"],
                d.get("version_id"), d.get("region_code")), 201

        def p_notification(self, _m, d):
            return self.app.identity.create_notification(
                self._role, d["notification_id"], d["reason_code"],
                d["analysis_ids"], d.get("risk_id")), 201

        def p_notification_deliver(self, m, d):
            return self.app.identity.deliver_notification(
                self._role, m.group(1), d.get("delivery_note", "")), 200

        def p_admin_key(self, _m, d):
            key = self.app.access.provision(d["role"], d["label"])
            return {"role": d["role"], "label": d["label"], "api_key": key}, 201

        def _authorize(self, permission):  # noqa: F811 - 保存鉴权角色
            key = self.headers.get("X-API-Key", "")
            role = self.app.access.authenticate(key)
            if role is None:
                raise StudyError("缺少或无效的 API 密钥", status=401)
            if not self.app.access.authorize(role, permission):
                raise StudyError(f"角色 {role} 无权执行 {permission}", status=403)
            self._role = role
            return role

    # (路径正则, 所需权限, 处理方法名)
    GET_ROUTES = [
        (r"/estimates", "estimate:read", "h_list_versions"),
        (r"/estimates/([^/]+)/verify", "estimate:read", "h_verify"),
        (r"/estimates/([^/]+)/regions", "estimate:read", "h_region_results"),
        (r"/estimates/([^/]+)", "estimate:read", "h_get_version"),
        (r"/feedback/([^/]+)", "feedback:read", "h_feedback"),
        (r"/ethics/versions/([^/]+)/regions/([^/]+)/trace", "lineage:read", "h_trace"),
        (r"/ethics/versions/([^/]+)/regions/([^/]+)/affected", "lineage:read", "h_affected"),
        (r"/notifications", "notification:read", "h_list_notifications"),
        (r"/notifications/([^/]+)", "notification:deliver", "h_notification_delivery"),
    ]
    POST_ROUTES = [
        (r"/households", "household:write", "p_household"),
        (r"/households/([^/]+)/move", "move:write", "p_move"),
        (r"/members", "member:write", "p_member"),
        (r"/members/([^/]+)/withdraw", "member:write", "p_withdraw"),
        (r"/consents", "consent:write", "p_consent"),
        (r"/consents/([^/]+)/revoke", "consent:write", "p_consent_revoke"),
        (r"/followups", "followup:write", "p_followup"),
        (r"/sites", "site:write", "p_site"),
        (r"/interventions", "site:write", "p_intervention"),
        (r"/devices", "device:write", "p_device"),
        (r"/calibrations", "calibration:write", "p_calibration"),
        (r"/calibrations/([^/]+)/invalidate", "calibration:write", "p_calibration_invalidate"),
        (r"/readings:bulk", "reading:write", "p_ingest"),
        (r"/timeline/build", "reading:write", "p_timeline_build"),
        (r"/estimates", "estimate:write", "p_estimate"),
        (r"/risks", "risk:write", "p_risk"),
        (r"/notifications", "notification:create", "p_notification"),
        (r"/notifications/([^/]+)/deliver", "notification:deliver", "p_notification_deliver"),
        (r"/admin/keys", "key:write", "p_admin_key"),
    ]
    return StudyHandler


def health_payload():
    return {"status": "ok", "service": "secondhand-smoke-study",
            "name": "二手烟暴露干预研究"}


def _parse_query(query):
    params = {}
    for pair in query.split("&"):
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v
        else:
            params[pair] = ""
    return params


def _as_bool(value):
    if value is None:
        return None
    return value.lower() in ("1", "true", "yes")
