"""二手烟暴露干预研究的运行入口。

- python3 service.py --check        核对服务配置（不落库）
- python3 service.py --port 8000    启动完整领域服务
- python3 service.py --init-keys    初始化五个角色的演示密钥并打印

领域代码位于 study 包；身份库与分析库路径可用
--identity-db / --analysis-db 覆盖（默认 identity.db / analysis.db）。
"""

import argparse
import json
from http.server import ThreadingHTTPServer

from study.api import make_handler

SERVICE_ID = "secondhand-smoke-study"
SERVICE_NAME = "二手烟暴露干预研究"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _build_handler(identity_path="identity.db", analysis_path="analysis.db"):
    def factory():
        from study.app import StudyApp
        return StudyApp(identity_path, analysis_path)

    return make_handler(factory)


# 供 service_contract 等模块在默认路径下导入；真正启动服务时 main()
# 会用命令行参数重建 Handler。
Handler = _build_handler()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--init-keys", action="store_true")
    parser.add_argument("--identity-db", default="identity.db")
    parser.add_argument("--analysis-db", default="analysis.db")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        from study.config import REGION_COUNT
        assert REGION_COUNT == 204
        print("基础检查通过")
        return

    handler = _build_handler(args.identity_db, args.analysis_db)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    app = None
    try:
        if args.init_keys:
            from study.app import StudyApp
            app = StudyApp(args.identity_db, args.analysis_db)
            server.study_app = app
            keys = app.init_demo_keys()
            print("演示密钥（仅本地联调用，请通过 POST /admin/keys 轮换）：")
            print(json.dumps(keys, ensure_ascii=False, indent=2))
        server.serve_forever()
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":
    main()
