"""防篡改审计日志。

所有触及身份库、校准失效、冻结、反馈与通知的动作都追加一条记录。
记录只增不改；``sealed`` 保存前一条记录的 SHA-256，形成哈希链，
任何事后删改都会在下一次校验时暴露。
"""

import hashlib
import json
import threading


def _fingerprint(record):
    blob = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self):
        self._records = []
        self._lock = threading.Lock()

    def append(self, actor, action, **detail):
        with self._lock:
            sealed = self._records[-1]["id"] if self._records else None
            record = {
                "seq": len(self._records) + 1,
                "actor": actor,
                "action": action,
                "detail": detail,
                "sealed": sealed,
            }
            record["id"] = _fingerprint(record)
            self._records.append(record)
            return {k: record[k] for k in ("seq", "actor", "action", "detail")}

    def entries(self, actor, action=None, limit=None):
        """读取审计记录。可按动作过滤（伦理溯源用）。"""
        with self._lock:
            records = list(self._records)
        if action is not None:
            records = [r for r in records if r["action"] == action]
        if limit is not None:
            records = records[-limit:]
        return [{k: r[k] for k in ("seq", "actor", "action", "detail")} for r in records]

    def verify(self):
        """重放哈希链，返回是否完整以及（若断裂）首个断裂位置。"""
        sealed = None
        for index, record in enumerate(self._records):
            expected_sealed = record["sealed"]
            if expected_sealed != sealed:
                return False, index + 1
            stored_id = record["id"]
            rebuilt = {k: v for k, v in record.items() if k != "id"}
            if _fingerprint(rebuilt) != stored_id:
                return False, index + 1
            sealed = stored_id
        return True, None
