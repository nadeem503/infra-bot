"""Active thread tracker.

When the bot is @mentioned in a thread, that thread is marked "active" in
Redis for a configurable TTL.  Subsequent messages in the same thread — even
without an @mention — are then processed by the bot as if it was mentioned.

Keys:
    infra:active_thread:{channel}:{thread_ts}  →  user_id who activated
    TTL: ACTIVE_THREAD_TTL_SECONDS (default 2 h)
"""
from __future__ import annotations

from utils.logger import get_logger
from .redis_client import get_redis

logger = get_logger(__name__)

_KEY_PREFIX = "infra:active_thread"
ACTIVE_THREAD_TTL_SECONDS = 3600   # 1 hour


def _key(channel: str, thread_ts: str) -> str:
    return f"{_KEY_PREFIX}:{channel}:{thread_ts}"


def activate(channel: str, thread_ts: str, activated_by: str = "") -> None:
    """Mark a thread as active. Called when the bot receives an @mention."""
    try:
        get_redis().setex(_key(channel, thread_ts), ACTIVE_THREAD_TTL_SECONDS, activated_by or "1")
        logger.debug("Thread activated: %s/%s by %s", channel, thread_ts, activated_by)
    except Exception as exc:  # noqa: BLE001
        logger.warning("active_threads.activate failed: %s", exc)


def is_active(channel: str, thread_ts: str) -> bool:
    """Return True if the bot has been @mentioned in this thread recently."""
    try:
        return bool(get_redis().exists(_key(channel, thread_ts)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("active_threads.is_active failed: %s", exc)
        return False


def deactivate(channel: str, thread_ts: str) -> None:
    """Explicitly deactivate a thread (e.g. if user says 'stop' or 'bye')."""
    try:
        get_redis().delete(_key(channel, thread_ts))
    except Exception as exc:  # noqa: BLE001
        logger.warning("active_threads.deactivate failed: %s", exc)
