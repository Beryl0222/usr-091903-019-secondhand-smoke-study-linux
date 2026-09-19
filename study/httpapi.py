"""HTTP 路由层：把 JSON 请求映射到领域服务。

鉴权简化为请求头 ``X-Actor-Role``（现场部署中由网关替换为真实令牌）。
所有写操作与敏感读取都经过领域层的角色校验并写审计。
"""

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from study.errors import StudyError
from study.server import health_payload


class ApiHandler(BaseHTTPRequestHandler):
    app = None  # 由 make_handler 注入

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False,
                          default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            from study.errors import ValidationError
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(payload, dict):
            from study.errors import ValidationError
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _actor(self, body):
        # 无角色头时归一化为 anonymous：公开目录可访问，
        # 任何受限操作都会被领域层的角色集合拒绝。
        return self.headers.get("X-Actor-Role") or body.pop("actor", None) \
            or "anonymous"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            parsed = urlsplit(self.path)
            if method == "GET" and parsed.path == "/health":
                self._send(200, health_payload())
                return
            body = self._read_body() if method == "POST" else {}
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            actor = self._actor({**body}) if parsed.path != "/health" else None
            status, result = self._route(
                method, parsed.path, query, body, actor)
            self._send(status, result)
        except StudyError as error:
            self._send(error.http_status, error.to_dict())
        except Exception as error:  # noqa: BLE001 - 统一兜底，避免泄漏堆栈
            self._send(500, {"error": "internal_error", "message": str(error)})

    def _route(self, method, path, query, body, actor):
        app = self.app
        c = app.catalog

        # ---- 目录 ------------------------------------------------------
        if method == "GET" and path == "/regions":
            return 200, {"regions": c.list_regions(),
                         "region_total": len(c.list_regions())}
        if method == "POST" and path == "/communities":
            self._require_role(actor, {"field", "admin"})
            return 201, c.add_community(
                body["community_id"], body["region_code"], body.get("name"))
        if method == "POST" and path == "/venues":
            self._require_role(actor, {"field", "admin"})
            return 201, c.add_venue(
                body["venue_type"], body["community_id"], body.get("label"))
        if method == "POST" and path == "/interventions":
            self._require_role(actor, {"field", "admin"})
            return 201, c.register_intervention(
                body["version"], body["effective_date"],
                body.get("description"))
        if method == "POST" and path == "/interventions/assign":
            self._require_role(actor, {"field", "admin"})
            return 200, c.assign_intervention(
                body["community_id"], body["version"], body.get("phase"))

        # ---- 家庭/成员/同意 --------------------------------------------
        v = app.vault
        if method == "POST" and path == "/households":
            return 201, v.create_household(
                actor, body["community_id"], body.get("contact_name"),
                body.get("contact_phone"), body.get("address"))
        if method == "POST" and path == "/members":
            return 201, v.add_member(
                actor, body["household_id"], body["birth_date"],
                body.get("sex"), body.get("name"), body.get("move_in"))
        if method == "POST" and path == "/consents/grant":
            return 201, v.grant_consent(
                actor, body["household_id"], body["start_date"],
                body.get("end_date"), body.get("document"))
        if method == "POST" and path == "/consents/end":
            return 200, v.end_consent(
                actor, body["household_id"], body["date"], body.get("reason"))
        if method == "POST" and path == "/members/withdraw":
            return 200, v.withdraw_member(
                actor, body["analysis_id"], body["date"], body.get("reason"))
        if method == "POST" and path == "/members/move":
            return 200, v.move_member(
                actor, body["analysis_id"], body["to_household_id"],
                body["date"])
        if method == "GET" and path == "/roster":
            return 200, {"members": v.analysis_roster(actor)}

        # ---- 传感器 ----------------------------------------------------
        s = app.sensors
        if method == "POST" and path == "/devices":
            return 201, s.register_device(
                actor, body["serial"], body.get("model"))
        if method == "POST" and path == "/calibrations":
            return 201, s.add_calibration(
                actor, body["serial"], body["valid_from"],
                body.get("valid_to"), body.get("reference"))
        if method == "POST" and path == "/calibrations/revoke":
            return 200, s.revoke_calibration(
                actor, body["calibration_id"], body["date"],
                body.get("reason"))
        if method == "POST" and path == "/deployments":
            return 201, s.deploy(
                actor, body["serial"], body["location_kind"],
                body["location_ref"], body["start_date"],
                body.get("end_date"), body.get("interval_minutes", 60))
        if method == "POST" and path == "/deployments/close":
            return 200, s.close_deployment(
                actor, body["serial"], body["date"])
        if method == "POST" and path == "/readings/batch":
            return 200, s.ingest_batch(
                actor, body["serial"], body["readings"])
        if method == "GET" and path == "/readings":
            self._require_role(
                actor, {"field", "analyst", "ethics", "admin"})
            return 200, {"readings": s.query_readings(
                location_ref=query.get("location_ref"),
                valid_only=query.get("valid_only") in ("1", "true"))}
        if method == "GET" and path == "/quality":
            self._require_role(
                actor, {"field", "analyst", "ethics", "admin"})
            return 200, s.quality_counts(query.get("location_ref"))

        # ---- 暴露与症状 ------------------------------------------------
        e = app.exposure
        if method == "POST" and path == "/exposure/materialize":
            return 200, e.materialize(
                actor, body["window_start"], body["window_end"])
        if method == "GET" and path == "/contributions":
            return 200, {"contributions": e.contributions(
                actor, region_code=query.get("region_code"),
                community_id=query.get("community_id"),
                measured_only=query.get("measured_only") in ("1", "true"))}
        if method == "POST" and path == "/symptoms":
            return 201, e.record_symptoms(
                actor, body["analysis_id"], body["symptom_date"],
                body["symptoms"], body.get("severity", "mild"),
                body.get("report_date"), body.get("notes"))
        if method == "GET" and path == "/symptoms":
            return 200, {"followups": e.symptom_followups(
                actor, analysis_id=query.get("analysis_id"),
                in_window_only=query.get("in_window_only") in ("1", "true"))}

        # ---- 估算与冻结 ------------------------------------------------
        es = app.estimates
        if method == "POST" and path == "/estimates/run":
            return 201, es.run(
                actor, body["window_start"], body["window_end"],
                body.get("weights"), body.get("label"))
        if method == "POST" and path == "/estimates/freeze":
            return 201, es.freeze(
                actor, body.get("run_id"), body.get("label"))
        if method == "GET" and path == "/freezes":
            return 200, {"freezes": es.list_freezes(actor)}

        m = re.fullmatch(r"/freezes/(FZ-\d+)", path)
        if method == "GET" and m:
            return 200, es.get_freeze(actor, m.group(1))
        m = re.fullmatch(r"/freezes/(FZ-\d+)/verify", path)
        if method == "POST" and m:
            return 200, es.verify_freeze(actor, m.group(1))
        m = re.fullmatch(r"/runs/(RUN-\d+)", path)
        if method == "GET" and m:
            return 200, es.get_run(actor, m.group(1))

        # ---- 社区反馈 / 伦理 / 通知 ------------------------------------
        f, eth, n = app.feedback, app.ethics, app.notifications
        m = re.fullmatch(r"/communities/([^/]+)/report", path)
        if method == "GET" and m:
            return 200, f.community_report(
                actor, m.group(1), query["freeze_id"])
        m = re.fullmatch(r"/ethics/freeze/(FZ-\d+)/overview", path)
        if method == "GET" and m:
            return 200, eth.aggregate_view(actor, m.group(1))
        m = re.fullmatch(
            r"/ethics/freeze/(FZ-\d+)/regions/(R\d{3})", path)
        if method == "GET" and m:
            return 200, eth.trace_region(actor, m.group(1), m.group(2))
        m = re.fullmatch(r"/ethics/analysis/(A-\d+)", path)
        if method == "GET" and m:
            return 200, eth.trace_analysis(actor, m.group(1))
        if method == "POST" and path == "/ethics/identity":
            return 200, eth.open_identity(
                actor, body["analysis_id"], body["reason"])
        if method == "POST" and path == "/notifications":
            return 201, n.issue(
                actor, body["analysis_ids"], body["reason"],
                body["advice"], body.get("channel", "field_visit"))
        if method == "GET" and path == "/notifications":
            return 200, {"notifications": n.list_for_field(
                actor, community_id=query.get("community_id"))}
        m = re.fullmatch(r"/notifications/(N-\d+)/deliver", path)
        if method == "POST" and m:
            return 200, n.mark_delivered(
                actor, m.group(1), body["household_id"],
                body.get("outcome", "delivered"))

        # ---- 审计 ------------------------------------------------------
        if method == "GET" and path == "/audit":
            role = actor
            if role not in {"ethics", "admin"}:
                from study.errors import PermissionDenied
                raise PermissionDenied("只有伦理/管理员可读取审计日志")
            return 200, {"entries": app.audit.entries(
                actor, action=query.get("action"),
                limit=int(query["limit"]) if query.get("limit") else None)}

        from study.errors import NotFound
        raise NotFound(f"未找到路由：{method} {path}")

    def log_message(self, *_args):
        return

    @staticmethod
    def _require_role(actor, allowed):
        if actor not in allowed:
            from study.errors import PermissionDenied
            raise PermissionDenied(
                f"角色 {actor} 无权执行该操作", allowed=sorted(allowed))
