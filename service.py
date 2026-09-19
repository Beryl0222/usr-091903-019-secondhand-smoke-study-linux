"""二手烟暴露干预研究的运行入口。

- ``python3 service.py --check``        核对服务配置与领域装配
- ``python3 service.py --port 8000``    仅健康检查
- ``python3 service.py --demo``         装配演示数据并开放全部领域接口

领域接口鉴权使用 ``X-Actor-Role`` 请求头（field/analyst/community/
ethics/admin），所有敏感动作均写入审计链。
"""

import argparse

from study.server import (
    SERVICE_ID,
    SERVICE_NAME,
    HealthHandler,
    build_server,
    health_payload,
)

# 向后兼容：契约测试从 service 导入 Handler 与服务常量。
Handler = HealthHandler


def run_check():
    assert health_payload()["service"] == SERVICE_ID
    # 装配演示链路，确保各领域模块可以完整串通。
    from study.app import build_demo_app
    app, freeze_id = build_demo_app()
    intact, broken_at = app.audit.verify()
    assert intact, f"审计链在第 {broken_at} 条断裂"
    assert freeze_id
    print("基础检查通过（含演示数据装配与审计链校验）")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="装配演示数据并开放领域接口")
    args = parser.parse_args()
    if args.check:
        run_check()
        return
    app = None
    if args.demo:
        from study.app import build_demo_app
        app, _freeze_id = build_demo_app()
        print(f"演示数据已装配，访问 http://127.0.0.1:{args.port}/health")
    build_server(args.port, app).serve_forever()


if __name__ == "__main__":
    main()
