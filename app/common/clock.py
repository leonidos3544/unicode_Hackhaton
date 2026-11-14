from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_reference_time: ContextVar[datetime | None] = ContextVar("catalog_reference_time", default=None)


def utcnow() -> datetime:
    return datetime.now(UTC)


def reference_now() -> datetime:
    """Business time; operational leases, session expiry and quotas keep real UTC time."""
    return _reference_time.get() or utcnow()


@contextmanager
def at_reference_time(moment: datetime) -> Iterator[None]:
    """Internal evaluation context. No HTTP parameter can alter the service clock."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Reference time must include a timezone")
    token = _reference_time.set(moment.astimezone(UTC))
    try:
        yield
    finally:
        _reference_time.reset(token)
