"""角色、API 密钥与最小权限矩阵。

密钥是运维凭证而非研究数据，单独表存放于身份库（敏感侧）。
角色边界：
- field_coordinator 现场协调员：登记家庭/成员/同意、随访、发起定向通知落地
- analyst 分析人员：场所/设备/校准、读数补传、生成并冻结估算
- community_responder 社区反馈员：仅读取经小单元格抑制的聚合反馈
- ethics_officer 伦理人员：血缘追溯、登记风险、发起定向通知
- admin 管理员：密钥管理
"""

import hashlib
import hmac
import secrets
import time

ROLES = (
    "field_coordinator",
    "analyst",
    "community_responder",
    "ethics_officer",
    "admin",
)

PERMISSIONS = {
    "field_coordinator": {
        "household:write", "member:write", "consent:write", "followup:write",
        "move:write",
        "notification:deliver",
    },
    "analyst": {
        "site:write", "device:write", "calibration:write", "reading:write",
        "estimate:write", "estimate:read",
    },
    "community_responder": {
        "feedback:read",
    },
    "ethics_officer": {
        "lineage:read", "risk:write", "notification:create", "notification:read",
        "estimate:read",
    },
    "admin": {
        "key:write",
    },
}


class Access:
    def __init__(self, identity_conn):
        self.conn = identity_conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS api_keys (
                   key_prefix TEXT PRIMARY KEY,
                   key_hash   TEXT NOT NULL,
                   role       TEXT NOT NULL,
                   label      TEXT NOT NULL,
                   active     INTEGER NOT NULL DEFAULT 1,
                   created_at TEXT NOT NULL
               )"""
        )

    def provision(self, role, label, key=None):
        if role not in ROLES:
            raise ValueError(f"未知角色: {role}")
        key = key or "shk_" + secrets.token_urlsafe(24)
        prefix = key[:12]
        digest = hmac.new(key.encode(), b"study-key", hashlib.sha256).hexdigest()
        self.conn.execute(
            "INSERT OR REPLACE INTO api_keys VALUES (?, ?, ?, ?, 1, ?)",
            (prefix, digest, role, label, _now()),
        )
        return key

    def authenticate(self, key):
        if not key:
            return None
        prefix = key[:12]
        row = self.conn.execute(
            "SELECT * FROM api_keys WHERE key_prefix=? AND active=1", (prefix,)
        ).fetchone()
        if row is None:
            return None
        digest = hmac.new(key.encode(), b"study-key", hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, row["key_hash"]):
            return None
        return row["role"]

    def authorize(self, role, permission):
        return role in PERMISSIONS and permission in PERMISSIONS[role]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
