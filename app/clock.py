"""Injectable clock so timeouts / backoff can be demonstrated without waiting."""
import time

_offset = 0.0


def now() -> float:
    return time.time() + _offset


def advance(seconds: float) -> float:
    global _offset
    _offset += float(seconds)
    return _offset


def reset() -> None:
    global _offset
    _offset = 0.0


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"
