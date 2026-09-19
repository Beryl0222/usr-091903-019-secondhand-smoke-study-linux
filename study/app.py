"""应用装配：把目录、身份库、传感器、暴露、估算与反馈服务组装起来。"""

from study.audit import AuditLog
from study.catalog import Catalog
from study.estimates import EstimateStore
from study.exposure import ExposureStore
from study.feedback import EthicsService, FeedbackService, NotificationService
from study.identity import IdentityVault
from study.sensors import SensorStore


class StudyApp:
    def __init__(self):
        self.audit = AuditLog()
        self.catalog = Catalog()
        self.vault = IdentityVault(self.catalog, self.audit)
        self.sensors = SensorStore(self.catalog, self.vault, self.audit)
        self.exposure = ExposureStore(
            self.catalog, self.vault, self.sensors, self.audit)
        self.estimates = EstimateStore(
            self.catalog, self.vault, self.sensors, self.exposure, self.audit)
        self.feedback = FeedbackService(
            self.catalog, self.vault, self.sensors, self.exposure,
            self.estimates, self.audit)
        self.ethics = EthicsService(
            self.catalog, self.vault, self.sensors, self.exposure,
            self.estimates, self.audit)
        self.notifications = NotificationService(
            self.catalog, self.vault, self.audit)


def build_demo_app():
    """构造一份覆盖完整链路的演示数据，供联调与冒烟测试使用。"""
    app = StudyApp()
    field, analyst, ethics = "field", "analyst", "ethics"

    # 一个社区位于 R001；控烟政策 2026-03-01 生效。
    app.catalog.add_community("C001", "R001", name="一社区")
    app.catalog.register_intervention(
        "v1", "2026-03-01", description="室内公共场所禁烟")
    app.catalog.assign_intervention("C001", "v1")
    venue = app.catalog.add_venue("restaurant", "C001", label="集市北门")

    # 两户家庭、各一名成员（一名幼儿、一名成人）。
    hh1 = app.vault.create_household(
        field, "C001", contact_name="甲", contact_phone="1001",
        address="一栋")["household_id"]
    hh2 = app.vault.create_household(
        field, "C001", contact_name="乙", contact_phone="1002",
        address="二栋")["household_id"]
    m1 = app.vault.add_member(field, hh1, "2023-01-01", name="甲幼")[
        "analysis_id"]
    m2 = app.vault.add_member(field, hh2, "1990-01-01", name="乙成")[
        "analysis_id"]
    app.vault.grant_consent(field, hh1, "2026-01-01")
    app.vault.grant_consent(field, hh2, "2026-01-01")

    # 设备：家庭机 + 场所机，校准覆盖全程。
    app.sensors.register_device(field, "SN-HOME", model="PMSx")
    app.sensors.register_device(field, "SN-VENUE", model="PMSx")
    app.sensors.add_calibration(field, "SN-HOME", "2026-01-01")
    app.sensors.add_calibration(field, "SN-VENUE", "2026-01-01")
    app.sensors.deploy(field, "SN-HOME", "household", hh1,
                       "2026-01-01", interval_minutes=1440)
    app.sensors.deploy(field, "SN-VENUE", "venue", venue["venue_id"],
                       "2026-01-01", interval_minutes=1440)

    def readings(serial, days_pm):
        batch = []
        for day, pm in days_pm:
            batch.append({"sample_start": f"{day}T00:00:00Z",
                          "sample_end": f"{day}T23:59:00Z", "pm25": pm})
        app.sensors.ingest_batch(field, serial, batch)

    pre_days = [f"2026-01-{d:02d}" for d in range(10, 15)]
    post_days = [f"2026-04-{d:02d}" for d in range(10, 15)]
    readings("SN-HOME", [(d, 35) for d in pre_days]
             + [(d, 15) for d in post_days])
    readings("SN-VENUE", [(d, 40) for d in pre_days]
             + [(d, 18) for d in post_days])

    # 让两个年龄段在两期都有数据（把成人家庭也部署上会更真实，
    # 演示数据里让 m2 与 hh1 同社区即可由人天行补齐需要第二户的测量：
    # 为简单起见，再登记一户测量）。
    app.sensors.register_device(field, "SN-HOME2", model="PMSx")
    app.sensors.add_calibration(field, "SN-HOME2", "2026-01-01")
    app.sensors.deploy(field, "SN-HOME2", "household", hh2,
                       "2026-01-01", interval_minutes=1440)
    readings("SN-HOME2", [(d, 33) for d in pre_days]
             + [(d, 16) for d in post_days])

    for serial in ("SN-HOME", "SN-VENUE", "SN-HOME2"):
        app.sensors.close_deployment(field, serial, "2026-05-01")
    app.exposure.materialize(analyst, "2026-01-01", "2026-05-01")
    # 演示数据只覆盖幼儿与成人两个年龄段，未覆盖段权重置 0，
    # 正式分析应由分析人员显式提供标准人口权重。
    weights = {"under_5": 0.5, "5_17": 0.0, "18_59": 0.5, "60_plus": 0.0}
    run = app.estimates.run(analyst, "2026-01-01", "2026-05-01", weights,
                            label="2026春季估算")
    freeze = app.estimates.freeze(analyst, run["run_id"],
                                  label="2026春季冻结")
    return app, freeze["freeze_id"]
