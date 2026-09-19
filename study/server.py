"""服务身份与 HTTP 服务器工厂。"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_ID = "secondhand-smoke-study"
SERVICE_NAME = "二手烟暴露干预研究"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class HealthHandler(BaseHTTPRequestHandler):
    """只暴露健康检查；未装配领域应用时的最小处理器。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def make_handler(app):
    """生成绑定了应用实例的 API 处理器类。"""
    from study.httpapi import ApiHandler

    class BoundHandler(ApiHandler):
        pass

    BoundHandler.app = app
    return BoundHandler


def build_server(port, app=None):
    if app is None:
        return ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
