"""空气传感器：设备登记、校准会话与读数补传。

关键规则：
1. 离线设备恢复后批量补传，按 (device_serial, window_start, window_end)
   唯一约束去重；重复窗口被跳过并计数，不覆盖原值（保证补传幂等）。
2. 每条读数写入时按"采样窗口中点"匹配当时生效的校准会话。
   校准失效（无有效会话、或会话被标记 invalid/expired）的读数
   valid_for_estimate=0 并记录 exclude_reason，但数据原样保留在库中。
3. 校准转换仅在估算阶段应用；原始 pm25 永不覆写。
"""

import time
from datetime import date

from .errors import ConflictError, NotFoundError, ValidationError


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SensorService:
    def __init__(self, conn, lock):
        self.conn = conn
        self.lock = lock

    # ---- 设备 ----------------------------------------------------------
    def register_device(self, device_serial, assign_type="site",
                        site_id=None, analysis_id=None, deployed_on=None):
        if assign_type not in ("site", "household"):
            raise ValidationError("assign_type 必须是 site 或 household")
        with self.lock:
            if self.conn.execute(
                "SELECT 1 FROM devices WHERE device_serial=?", (device_serial,)
            ).fetchone():
                raise ConflictError(f"设备已登记: {device_serial}")
            if assign_type == "site":
                if site_id is None or self.conn.execute(
                    "SELECT 1 FROM sites WHERE site_id=?", (site_id,)
                ).fetchone() is None:
                    raise NotFoundError("场所设备必须关联已登记场所")
            self.conn.execute(
                "INSERT INTO devices(device_serial, assign_type, site_id, analysis_id, deployed_on)"
                " VALUES (?,?,?,?,?)",
                (device_serial, assign_type, site_id, analysis_id, deployed_on or _today()),
            )
        return {"device_serial": device_serial, "assign_type": assign_type}

    # ---- 校准 ----------------------------------------------------------
    def add_calibration(self, session_id, device_serial, calibrated_at,
                        valid_from, valid_to=None, gain=1.0, offset=0.0,
                        status="valid"):
        if status not in ("valid", "invalid"):
            raise ValidationError("status 必须是 valid 或 invalid")
        if valid_to is not None and valid_to < valid_from:
            raise ValidationError("valid_to 不得早于 valid_from")
        with self.lock:
            if self.conn.execute(
                "SELECT 1 FROM devices WHERE device_serial=?", (device_serial,)
            ).fetchone() is None:
                raise NotFoundError(f"设备不存在: {device_serial}")
            # 同设备有效期重叠的校准不允许
            overlap = self.conn.execute(
                "SELECT 1 FROM calibration_sessions WHERE device_serial=? "
                "AND NOT (valid_to IS NOT NULL AND valid_to < ?) "
                "AND NOT (? IS NOT NULL AND valid_from > ?) LIMIT 1",
                (device_serial, valid_from, valid_to, valid_to),
            ).fetchone()
            if overlap:
                raise ConflictError("与该设备已有校准会话的有效期重叠")
            try:
                self.conn.execute(
                    "INSERT INTO calibration_sessions VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, device_serial, calibrated_at, valid_from, valid_to,
                     gain, offset, status),
                )
            except Exception as exc:
                raise ConflictError(f"校准会话已存在: {session_id}") from exc
        return {"session_id": session_id, "device_serial": device_serial,
                "valid_from": valid_from, "valid_to": valid_to, "status": status}

    def invalidate_calibration(self, session_id):
        """把校准会话标记为 invalid；已写入的读数保留，其有效性在估算时复核。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT status FROM calibration_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"校准会话不存在: {session_id}")
            self.conn.execute(
                "UPDATE calibration_sessions SET status='invalid' WHERE session_id=?",
                (session_id,),
            )
        return {"session_id": session_id, "status": "invalid"}

    def session_for_window(self, device_serial, window_start, window_end):
        midpoint = _midpoint(window_start, window_end)
        return self.conn.execute(
            "SELECT * FROM calibration_sessions WHERE device_serial=? "
            "AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?) "
            "ORDER BY status DESC, calibrated_at DESC",
            (device_serial, midpoint, midpoint),
        ).fetchone()

    # ---- 读数补传 ------------------------------------------------------
    def ingest_batch(self, device_serial, samples, ingest_batch=None):
        """批量补传。samples: [{window_start, window_end, pm25}, ...]

        返回 {accepted, duplicates, excluded, items:[...]}，整个批次
        在一个事务内，调用方重试整批也是安全的（重复窗口全部跳过）。
        """
        if not samples:
            raise ValidationError("samples 为空")
        ingest_batch = ingest_batch or f"bat-{int(time.time()*1000)}-{_token()}"
        accepted = duplicates = 0
        items = []
        with self.lock:
            if self.conn.execute(
                "SELECT 1 FROM devices WHERE device_serial=?", (device_serial,)
            ).fetchone() is None:
                raise NotFoundError(f"设备不存在: {device_serial}")
            self.conn.execute("BEGIN")
            try:
                for s in samples:
                    ws, we, pm = s["window_start"], s["window_end"], float(s["pm25"])
                    if we <= ws:
                        raise ValidationError(f"非法采样窗口: {ws}..{we}")
                    # 先尝试查重（同一批次内重复也会命中唯一约束）
                    existing = self.conn.execute(
                        "SELECT reading_id FROM readings "
                        "WHERE device_serial=? AND window_start=? AND window_end=?",
                        (device_serial, ws, we),
                    ).fetchone()
                    if existing is not None:
                        duplicates += 1
                        items.append({"window_start": ws, "window_end": we,
                                      "status": "duplicate",
                                      "reading_id": existing["reading_id"]})
                        continue
                    session = self.session_for_window(device_serial, ws, we)
                    session_id, valid, reason = self._classify(session)
                    cur = self.conn.execute(
                        "INSERT INTO readings(device_serial, window_start, window_end,"
                        " pm25, received_at, ingest_batch, session_id,"
                        " valid_for_estimate, exclude_reason) VALUES (?,?,?,?,?,?,?,?,?)",
                        (device_serial, ws, we, pm, _now(), ingest_batch,
                         session_id, 1 if valid else 0, reason),
                    )
                    accepted += 1
                    items.append({"window_start": ws, "window_end": we,
                                  "status": "accepted" if valid else "excluded",
                                  "reading_id": cur.lastrowid,
                                  "exclude_reason": reason})
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        excluded = sum(1 for i in items if i["status"] == "excluded")
        return {"device_serial": device_serial, "ingest_batch": ingest_batch,
                "accepted": accepted, "duplicates": duplicates,
                "excluded": excluded, "items": items}

    @staticmethod
    def _classify(session):
        if session is None:
            return None, False, "no_calibration"
        if session["status"] != "valid":
            return session["session_id"], False, "calibration_invalid"
        return session["session_id"], True, None

    def calibrated_value(self, reading_row):
        """估算阶段调用：对有效读数应用校准线性变换。"""
        if not reading_row["valid_for_estimate"]:
            return None
        session = self.conn.execute(
            "SELECT * FROM calibration_sessions WHERE session_id=?",
            (reading_row["session_id"],),
        ).fetchone()
        # 二次复核：读数入库后校准可能已失效
        if session is None or session["status"] != "valid":
            return None
        return reading_row["pm25"] * session["gain"] + session["offset"]


def _midpoint(window_start, window_end):
    """ISO 时间戳窗口的中点（UTC），用于匹配当日生效的校准会话。"""
    import datetime
    start = datetime.datetime.fromisoformat(window_start.replace("Z", "+00:00"))
    end = datetime.datetime.fromisoformat(window_end.replace("Z", "+00:00"))
    mid = start + (end - start) / 2
    return mid.date().isoformat()


def _today():
    return date.today().isoformat()


def _token():
    import secrets
    return secrets.token_hex(4)
