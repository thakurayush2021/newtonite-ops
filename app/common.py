"""Shared helpers: time, errors, validation."""
from datetime import datetime, timezone, timedelta

TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"   # fixed-width UTC => lexicographic order == chronological order


def now_dt():
    return datetime.now(timezone.utc)


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def now_iso():
    return to_iso(now_dt())


def iso_in(seconds):
    return to_iso(now_dt() + timedelta(seconds=seconds))


class ApiError(Exception):
    """Raised anywhere in the stack; rendered as a JSON error with a stable machine-readable `code`."""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def validation(message, field=None):
    return ApiError(400, "validation_error", message, {"field": field} if field else None)


def clean_str(body, key, *, required=False, max_len=200, min_len=1, default=None):
    v = body.get(key, default)
    if v is None:
        if required:
            raise validation(f"{key} is required", key)
        return None
    if not isinstance(v, str):
        raise validation(f"{key} must be a string", key)
    v = v.strip()
    if required and len(v) < min_len:
        raise validation(f"{key} must not be empty", key)
    if len(v) > max_len:
        raise validation(f"{key} must be at most {max_len} characters", key)
    return v


def parse_due(value):
    """Accepts None, 'YYYY-MM-DD' (end of that day, UTC) or an ISO-8601 timestamp. Returns normalised ISO or None."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise validation("due_at must be an ISO date or timestamp", "due_at")
    try:
        if len(value) == 10:
            d = datetime.strptime(value, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        else:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
    except ValueError:
        raise validation("due_at must be an ISO date or timestamp", "due_at")
    return to_iso(d)
