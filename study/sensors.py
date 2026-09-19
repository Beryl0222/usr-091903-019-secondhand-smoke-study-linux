"""空气传感器：设备登记、部署、校准窗口与离线补传去重。

关键规则：

- 去重键为 ``(设备序列, 采样窗口起, 采样窗口止)``。离线设备补传时，
  重复窗口被识别并跳过，不会重复累计暴露；
- 校准按时间窗口生效。校准被撤销或读数落在窗口外时，读数仍然保留，
  但 ``calibration_status`` 标为 ``invalid``，正式估算只取 ``valid``；
- 读数归属按采样时刻的部署记录解析，设备换点不影响历史读数的归属。
"""

import threading
from datetime import timedelta

from study.errors import Conflict, NotFound, ValidationError
from study.util import iso, new_id, parse_date, parse_ts


class SensorStore:
    def __init__(self, catalog, vault, audit):
        self._catalog = catalog
        self._vault = vault
        self._audit = audit
        self._lock = threading.Lock()
        self._devices = {}        # serial -> device
        self._calibrations = {}   # calibration_id -> record
        self._deployments = []    # 有序部署片段
        self._readings = {}       # (serial, start, end) -> reading

    # ---- 设备 ----------------------------------------------------------
    def register_device(self, actor, serial, model=None):
        with self._lock:
            if serial in self._devices:
                raise Conflict(f"设备序列已存在：{serial}")
            self._devices[serial] = {"serial": serial, "model": model,
                                     "status": "active"}
        self._audit.append(actor, "device_registered", serial=serial, model=model)
        return {"serial": serial, "model": model, "status": "active"}

    def _device(self, serial):
        device = self._devices.get(serial)
        if device is None:
            raise NotFound(f"设备未登记：{serial}")
        return device

    # ---- 校准窗口 ------------------------------------------------------
    def add_calibration(self, actor, serial, valid_from, valid_to=None,
                        reference=None):
        self._device(serial)
        start = parse_date(valid_from)
        end = parse_date(valid_to)
        if end is not None and end <= start:
            raise ValidationError("校准失效日必须晚于生效日")
        with self._lock:
            for existing in self._calibrations.values():
                if existing["serial"] != serial or existing["revoked_on"]:
                    continue
                if self._windows_overlap(
                    (start, end),
                    (parse_date(existing["valid_from"]),
                     parse_date(existing["valid_to"])),
                ):
                    raise Conflict("该设备存在时间重叠的有效校准窗口")
            calibration_id = new_id("CAL", self._calibrations)
            record = {
                "calibration_id": calibration_id,
                "serial": serial,
                "valid_from": iso(start),
                "valid_to": iso(end),
                "reference": reference,
                "revoked_on": None,
                "revoke_reason": None,
            }
            self._calibrations[calibration_id] = record
        self._audit.append(actor, "calibration_added",
                           calibration_id=calibration_id, serial=serial,
                           valid_from=iso(start), valid_to=iso(end))
        return dict(record)

    def revoke_calibration(self, actor, calibration_id, date, reason=None):
        """撤销校准：历史读数保留，但自撤销认定后一律不得进入正式估算。"""
        with self._lock:
            record = self._calibrations.get(calibration_id)
            if record is None:
                raise NotFound(f"校准记录不存在：{calibration_id}")
            if record["revoked_on"]:
                raise Conflict("该校准已被撤销")
            record["revoked_on"] = iso(parse_date(date))
            record["revoke_reason"] = reason
        self._audit.append(actor, "calibration_revoked",
                           calibration_id=calibration_id,
                           date=iso(parse_date(date)), reason=reason)
        return dict(record)

    @staticmethod
    def _windows_overlap(a, b):
        lo = max(a[0], b[0])
        ends = [d for d in (a[1], b[1]) if d is not None]
        hi = min(ends) if ends else None
        return hi is None or lo < hi

    def calibration_for(self, serial, sample_ts):
        """返回读数采样时刻适用的校准记录；无有效校准返回 None。"""
        sample_day = sample_ts.date()
        for record in self._calibrations.values():
            if record["serial"] != serial or record["revoked_on"]:
                continue
            start = parse_date(record["valid_from"])
            end = parse_date(record["valid_to"])
            if sample_day >= start and (end is None or sample_day < end):
                return dict(record)
        return None

    # ---- 部署 ----------------------------------------------------------
    def deploy(self, actor, serial, location_kind, location_ref,
               start_date, end_date=None, interval_minutes=60):
        """把设备部署到匿名场所（venue）或家庭（household）。

        同一设备的部署区间不得重叠；换点必须先闭合旧部署。
        """
        self._device(serial)
        if location_kind not in ("venue", "household"):
            raise ValidationError("location_kind 只能是 venue 或 household")
        if location_kind == "venue":
            self._catalog.get_venue(location_ref)
        else:
            self._vault.get_household(location_ref)
        start = parse_date(start_date)
        end = parse_date(end_date)
        if end is not None and end <= start:
            raise ValidationError("部署结束日必须晚于开始日")
        if interval_minutes <= 0:
            raise ValidationError("采样间隔必须为正数")
        with self._lock:
            for existing in self._deployments:
                if existing["serial"] != serial:
                    continue
                if self._windows_overlap(
                    (start, end),
                    (parse_date(existing["start_date"]),
                     parse_date(existing["end_date"])),
                ):
                    raise Conflict("该设备在重叠时段已有部署")
            record = {
                "deployment_id": f"DEP-{len(self._deployments) + 1:04d}",
                "serial": serial,
                "location_kind": location_kind,
                "location_ref": location_ref,
                "start_date": iso(start),
                "end_date": iso(end),
                "interval_minutes": interval_minutes,
            }
            self._deployments.append(record)
            self._deployments.sort(key=lambda r: r["start_date"])
        self._audit.append(actor, "device_deployed", serial=serial,
                           location_kind=location_kind,
                           location_ref=location_ref,
                           start=iso(start), end=iso(end))
        return dict(record)

    def close_deployment(self, actor, serial, date):
        day = parse_date(date)
        with self._lock:
            for record in self._deployments:
                if record["serial"] == serial and record["end_date"] is None:
                    if day <= parse_date(record["start_date"]):
                        raise ValidationError("撤点日期必须晚于部署开始日")
                    record["end_date"] = iso(day)
                    self._audit.append(actor, "deployment_closed",
                                       serial=serial, date=iso(day))
                    return dict(record)
        raise Conflict("该设备没有开放的部署记录")

    def deployment_at(self, serial, sample_ts):
        day = sample_ts.date()
        for record in self._deployments:
            if record["serial"] != serial:
                continue
            start = parse_date(record["start_date"])
            end = parse_date(record["end_date"])
            if day >= start and (end is None or day < end):
                return dict(record)
        return None

    def deployments(self):
        return [dict(r) for r in self._deployments]

    # ---- 读数补传与去重 ------------------------------------------------
    def ingest_batch(self, actor, serial, readings):
        """离线补传一批读数。

        readings: [{"sample_start": ISO, "sample_end": ISO, "pm25": 数}, ...]
        返回受理/重复计数；重复窗口不会覆盖首条数据。
        """
        self._device(serial)
        accepted = []
        duplicates = []
        for raw in readings:
            start = parse_ts(raw.get("sample_start"))
            end = parse_ts(raw.get("sample_end"))
            if end <= start:
                raise ValidationError("采样窗口结束必须晚于开始",
                                      sample_start=raw.get("sample_start"))
            pm25 = raw.get("pm25")
            if not isinstance(pm25, (int, float)) or pm25 < 0:
                raise ValidationError("pm25 必须为非负数值",
                                      sample_start=raw.get("sample_start"))
            key = (serial, start.isoformat(), end.isoformat())
            with self._lock:
                if key in self._readings:
                    existing = self._readings[key]
                    if existing["pm25"] != pm25:
                        raise Conflict(
                            "同一设备与采样窗口出现不同读数，需人工核查",
                            serial=serial, window=key[1])
                    duplicates.append(key[1])
                    continue
                deployment = self.deployment_at(serial, start)
                if deployment is None:
                    raise ValidationError(
                        "读数采样时刻没有有效部署，无法归属",
                        serial=serial, sample_start=key[1])
                calibration = self.calibration_for(serial, start)
                record = {
                    "serial": serial,
                    "sample_start": start.isoformat(),
                    "sample_end": end.isoformat(),
                    "pm25": float(pm25),
                    "location_kind": deployment["location_kind"],
                    "location_ref": deployment["location_ref"],
                    "calibration_id": (calibration or {}).get("calibration_id"),
                    "calibration_status": "valid" if calibration else "invalid",
                }
                self._readings[key] = record
                accepted.append(key[1])
        if accepted:
            self._audit.append(actor, "readings_ingested", serial=serial,
                               accepted=len(accepted),
                               duplicates=len(duplicates))
        return {"serial": serial,
                "accepted": len(accepted),
                "duplicates": len(duplicates)}

    def reading_view(self, record):
        view = dict(record)
        # 校准状态在读取时实时重算，撤销校准会即时影响可用判定。
        sample_ts = parse_ts(record["sample_start"])
        calibration = None
        if record["calibration_id"]:
            calibration = self._calibrations.get(record["calibration_id"])
        valid = (
            calibration is not None
            and not calibration["revoked_on"]
            and parse_date(calibration["valid_from"]) <= sample_ts.date()
            and (calibration["valid_to"] is None
                 or sample_ts.date() < parse_date(calibration["valid_to"]))
        )
        view["calibration_status"] = "valid" if valid else "invalid"
        return view

    def query_readings(self, location_ref=None, valid_only=False):
        """查询读数。valid_only=True 时只返回校准有效读数（正式估算口径）。"""
        rows = []
        for record in self._readings.values():
            view = self.reading_view(record)
            if location_ref is not None and view["location_ref"] != location_ref:
                continue
            if valid_only and view["calibration_status"] != "valid":
                continue
            rows.append(view)
        rows.sort(key=lambda r: r["sample_start"])
        return rows

    def quality_counts(self, location_ref=None):
        """按部署应测窗口统计：有效读数 / 失效读数 / 缺测窗口。"""
        valid = invalid = 0
        for record in self._readings.values():
            if location_ref is not None and record["location_ref"] != location_ref:
                continue
            view = self.reading_view(record)
            if view["calibration_status"] == "valid":
                valid += 1
            else:
                invalid += 1
        missing = self._expected_slot_count(location_ref) - valid - invalid
        return {"valid_readings": valid,
                "invalid_readings": invalid,
                "missing_windows": max(missing, 0)}

    def _expected_slot_count(self, location_ref):
        """根据部署区间与采样间隔计算应有窗口数（按整日折算）。"""
        total = 0
        for deployment in self._deployments:
            if location_ref is not None and deployment["location_ref"] != location_ref:
                continue
            end = parse_date(deployment["end_date"])
            # 开放部署只统计到今天，便于巡检当前缺测。
            if end is None:
                from datetime import date
                end = date.today()
            days = max((end - parse_date(deployment["start_date"])).days, 0)
            windows_per_day = (24 * 60) // deployment["interval_minutes"]
            total += days * windows_per_day
        return total

    # ---- 缺测窗口枚举（供估算/数据质量核查） ----------------------------
    def missing_slots(self, serial, day):
        """列出某设备某日缺测的采样窗口起点（按部署间隔推算）。"""
        deployment = next(
            (d for d in self._deployments
             if d["serial"] == serial
             and parse_date(d["start_date"]) <= day
             and (d["end_date"] is None or day < parse_date(d["end_date"]))),
            None,
        )
        if deployment is None:
            return []
        existing = set()
        for record in self._readings.values():
            ts = parse_ts(record["sample_start"])
            if record["serial"] == serial and ts.date() == day:
                existing.add(ts.replace(minute=ts.minute
                                        // deployment["interval_minutes"]
                                        * deployment["interval_minutes"]))
        slots = []
        step = timedelta(minutes=deployment["interval_minutes"])
        cursor = parse_ts(f"{iso(day)}T00:00:00+00:00")
        for _ in range((24 * 60) // deployment["interval_minutes"]):
            if cursor not in existing:
                slots.append(cursor.isoformat())
            cursor += step
        return slots
