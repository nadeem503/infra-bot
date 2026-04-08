"""Circuit breaker: pause actions on a host after N consecutive failures.

If 3 SSH/ADB actions fail on the same host within 15 minutes,
the circuit trips and further actions on that host are blocked for 10 minutes.

Usage:
    if circuit_breaker.is_tripped(host):
        # block action, post warning
    ...
    tripped = circuit_breaker.record_failure(host)
    if tripped:
        # notify channel
    circuit_breaker.record_success(host)  # reset on success
"""
from __future__ import annotations

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

FAILURE_THRESHOLD = 3
WINDOW_SECONDS = 900    # 15-min failure window
TRIP_DURATION = 600     # 10-min pause

_FAIL_KEY = "infra:circuit:fail:{host}"
_TRIP_KEY = "infra:circuit:trip:{host}"


def is_tripped(host: str) -> bool:
    if not host:
        return False
    return bool(get_redis().exists(_TRIP_KEY.format(host=host)))


def trip_ttl(host: str) -> int:
    """Seconds remaining until circuit resets. -1 if not tripped."""
    return get_redis().ttl(_TRIP_KEY.format(host=host))


def record_failure(host: str) -> bool:
    """Increment failure count. Returns True if circuit just tripped."""
    if not host:
        return False
    r = get_redis()
    fail_key = _FAIL_KEY.format(host=host)
    count = r.incr(fail_key)
    if count == 1:
        r.expire(fail_key, WINDOW_SECONDS)
    logger.debug("Circuit failure %d/%d for %s", count, FAILURE_THRESHOLD, host)
    if count >= FAILURE_THRESHOLD:
        r.setex(_TRIP_KEY.format(host=host), TRIP_DURATION, "1")
        r.delete(fail_key)
        logger.warning("Circuit breaker TRIPPED for %s", host)
        return True
    return False


def record_success(host: str) -> None:
    """Reset failure counter after a successful action."""
    if not host:
        return
    get_redis().delete(_FAIL_KEY.format(host=host))
