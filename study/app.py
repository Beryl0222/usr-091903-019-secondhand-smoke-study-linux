"""应用装配：双库 + 各领域服务的单一组合根。"""

from .access import ROLES, Access
from .database import Databases
from .estimation import EstimationService
from .ethics import EthicsService
from .feedback import FeedbackService
from .identity import IdentityService
from .sensors import SensorService
from .sites import SiteService
from .timeline import TimelineBuilder

# 仅用于本地初始化的演示密钥（--init-keys 时写入），生产应由 admin 轮换。
DEMO_KEYS = {
    "field_coordinator": ("shk_demo-field-0000000000000000", "现场协调员（演示）"),
    "analyst": ("shk_demo-analyst-000000000000000", "分析人员（演示）"),
    "community_responder": ("shk_demo-community-00000000", "社区反馈员（演示）"),
    "ethics_officer": ("shk_demo-ethics-00000000000000", "伦理人员（演示）"),
    "admin": ("shk_demo-admin-00000000000000000", "管理员（演示）"),
}


class StudyApp:
    def __init__(self, identity_path="identity.db", analysis_path="analysis.db"):
        self.db = Databases(identity_path, analysis_path)
        self.access = Access(self.db.identity)
        self.identity = IdentityService(self.db.identity, self.db.ilock)
        self.sites = SiteService(self.db.analysis, self.db.alock)
        self.sensors = SensorService(self.db.analysis, self.db.alock)
        self.timeline = TimelineBuilder(self.identity, self.db.analysis, self.db.alock)
        self.estimation = EstimationService(
            self.db.analysis, self.db.alock, self.identity, self.sensors)
        self.feedback = FeedbackService(self.db.analysis)
        self.ethics = EthicsService(self.db.analysis, self.identity)

    def init_demo_keys(self):
        result = {}
        for role in ROLES:
            key, label = DEMO_KEYS[role]
            self.access.provision(role, label, key=key)
            result[role] = key
        return result

    def close(self):
        self.db.close()
