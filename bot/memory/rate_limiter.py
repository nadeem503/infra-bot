"""Rate limiter: soft-block users who trigger too many infra actions in a window.

Threshold: 5 actions per user per 10-minute window.
On breach, returns a friendly warning instead of creating another approval flow.
"""
from __future__ import annotations

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 600   # 10-minute window
MAX_ACTIONS = 5


def check_and_increment(user_id: str) -> tuple[bool, int]:
    """Returns (allowed, current_count). Increments counter if within limit."""
    r = get_redis()
    key = f"infra:ratelimit:{user_id}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, WINDOW_SECONDS)
    allowed = count <= MAX_ACTIONS
    if not allowed:
        logger.warning("Rate limit hit: user=%s count=%d", user_id, count)
    return allowed, count


def get_last_action(user_id: str) -> str:
    return get_redis().get(f"infra:ratelimit:last:{user_id}") or "unknown"


def set_last_action(user_id: str, action_type: str) -> None:
    get_redis().setex(f"infra:ratelimit:last:{user_id}", WINDOW_SECONDS, action_type)


def ttl_remaining(user_id: str) -> int:
    return get_redis().ttl(f"infra:ratelimit:{user_id}")
