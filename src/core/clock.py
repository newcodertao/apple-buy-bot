from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def diagnostics(target: datetime | None, timezone: str = "Asia/Shanghai") -> dict:
    """Use local OS time only; timezone offset is not an estimate of clock skew."""
    zone = ZoneInfo(timezone)
    now_utc = datetime.now(UTC)
    local = now_utc.astimezone(zone)
    if target is not None and (target.tzinfo is None or target.utcoffset() is None):
        raise ValueError("Target time must include a timezone")
    return {
        "local_time": local.isoformat(timespec="milliseconds"),
        "utc_time": now_utc.isoformat(timespec="milliseconds"),
        "target_time": target.astimezone(zone).isoformat() if target else None,
        "timezone": timezone,
        "utc_offset_seconds": local.utcoffset().total_seconds(),
        "time_until_target_seconds": (target - now_utc).total_seconds() if target else None,
        "external_clock_skew_seconds": None,
        "clock_source": "local operating system; external clock skew not measured",
    }
