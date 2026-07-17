import time
from datetime import datetime

_last_attempt_at: dict[str, datetime] = {}


def mark_attempt(key: str) -> None:
    _last_attempt_at[key] = datetime.utcnow()


def seconds_since_last_attempt(key: str) -> float | None:
    last = _last_attempt_at.get(key)
    if last is None:
        return None
    return (datetime.utcnow() - last).total_seconds()


_last_nominatim_call_at: datetime | None = None


def wait_for_nominatim_slot(min_interval_seconds: float = 1.1) -> None:
    """Blocks briefly if needed to keep Nominatim calls under ~1/sec, per their usage policy."""
    global _last_nominatim_call_at
    now = datetime.utcnow()
    if _last_nominatim_call_at is not None:
        elapsed = (now - _last_nominatim_call_at).total_seconds()
        if elapsed < min_interval_seconds:
            time.sleep(min_interval_seconds - elapsed)
    _last_nominatim_call_at = datetime.utcnow()
