"""测试共用的研究场景构造。

场景：社区 C001（地区 R001）政策 2026-03-01 生效；两户家庭，
hh1 一名幼儿（2023 年生），hh2 一名成人（1990 年生）；
三台设备分别部署在两户与一个餐馆场所，日采样（1440 分钟）。
"""

from study.app import StudyApp

WEIGHTS = {"under_5": 0.5, "5_17": 0.0, "18_59": 0.5, "60_plus": 0.0}
PRE_DAYS = [f"2026-01-{d:02d}" for d in range(10, 15)]
POST_DAYS = [f"2026-04-{d:02d}" for d in range(10, 15)]


def build_scenario(with_readings=True, pm_pre=35.0, pm_post=15.0):
    app = StudyApp()
    field, analyst, ethics = "field", "analyst", "ethics"
    app.catalog.add_community("C001", "R001", name="一社区")
    app.catalog.register_intervention("v1", "2026-03-01",
                                      description="室内禁烟")
    app.catalog.assign_intervention("C001", "v1")
    venue = app.catalog.add_venue("restaurant", "C001", label="集市北门")

    hh1 = app.vault.create_household(
        field, "C001", contact_name="甲", contact_phone="1001",
        address="一栋")["household_id"]
    hh2 = app.vault.create_household(
        field, "C001", contact_name="乙", contact_phone="1002",
        address="二栋")["household_id"]
    a1 = app.vault.add_member(field, hh1, "2023-01-01",
                              name="甲幼")["analysis_id"]
    a2 = app.vault.add_member(field, hh2, "1990-01-01",
                              name="乙成")["analysis_id"]
    app.vault.grant_consent(field, hh1, "2026-01-01")
    app.vault.grant_consent(field, hh2, "2026-01-01")

    devices = [
        ("SN-1", "household", hh1),
        ("SN-2", "household", hh2),
        ("SN-3", "venue", venue["venue_id"]),
    ]
    for serial, kind, ref in devices:
        app.sensors.register_device(field, serial)
        app.sensors.add_calibration(field, serial, "2026-01-01")
        app.sensors.deploy(field, serial, kind, ref, "2026-01-01",
                           interval_minutes=1440)

    def feed(serial, days, pm):
        app.sensors.ingest_batch(field, serial, [{
            "sample_start": f"{day}T00:00:00Z",
            "sample_end": f"{day}T23:59:00Z",
            "pm25": pm,
        } for day in days])

    if with_readings:
        for serial, _kind, _ref in devices:
            feed(serial, PRE_DAYS, pm_pre)
            feed(serial, POST_DAYS, pm_post)

    ctx = {
        "app": app, "field": field, "analyst": analyst, "ethics": ethics,
        "community_id": "C001", "venue_id": venue["venue_id"],
        "hh1": hh1, "hh2": hh2, "a1": a1, "a2": a2,
        "serials": [d[0] for d in devices],
    }
    return ctx


def materialized(ctx, window_start="2026-01-01", window_end="2026-05-01"):
    app = ctx["app"]
    # 先闭合部署，缺测窗口才会限定在研究窗口而非“截至今天”。
    for serial in ctx["serials"]:
        app.sensors.close_deployment(ctx["field"], serial, window_end)
    app.exposure.materialize(ctx["analyst"], window_start, window_end)
    return app
