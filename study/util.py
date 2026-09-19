"""日期区间与编号工具。

研究只精确到“日”：所有截断（同意、搬家、退出、政策生效）都按真实日期比较。
区间统一为半开 ``[start, end)``，``end`` 为 ``None`` 表示持续至今。
"""

from datetime import date, datetime, timezone

from study.errors import ValidationError

DATE_FMT = "%Y-%m-%d"


def parse_date(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.strptime(str(value), DATE_FMT).date()
    except (ValueError, TypeError):
        raise ValidationError(f"日期格式应为 YYYY-MM-DD：{value!r}")


def iso(value):
    return None if value is None else value.strftime(DATE_FMT)


def parse_ts(value):
    """接受 ISO-8601（含 Z），返回时区感知 UTC datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValidationError(f"时间戳格式无法解析：{value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def intersect(start_a, end_a, start_b, end_b):
    """两个半开日期区间的交集；不相交返回 None。"""
    lo = max(start_a, start_b)
    hi_choices = [d for d in (end_a, end_b) if d is not None]
    hi = min(hi_choices) if hi_choices else None
    if hi is not None and lo >= hi:
        return None
    return lo, hi


def member_age_band(birth_date, on_date):
    """按真实日期计算周岁所在年龄段。"""
    age = on_date.year - birth_date.year - (
        (on_date.month, on_date.day) < (birth_date.month, birth_date.day)
    )
    if age < 5:
        return "under_5"
    if age < 18:
        return "5_17"
    if age < 60:
        return "18_59"
    return "60_plus"


def new_id(prefix, records):
    """生成人类可读的顺序编号，例如 HH-0001。"""
    return f"{prefix}-{len(records) + 1:04d}"
