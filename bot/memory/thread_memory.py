"""Thread conversation memory backed by Redis.

Stores last N messages per Slack thread so Claude has context
for follow-up messages like "also reboot it" or "what's the status?".
"""
from __future__ import annotations

import json
import time
from typing import Any

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

THREAD_TTL = 86400    # 24 hours
MAX_MESSAGES = 10     # per thread


def _key(channel: str, thread_ts: str) -> str:
    return f"infra:thread:{channel}:{thread_ts}"


def add_message(channel: str, thread_ts: str, role: str, content: str) -> None:
    """Append a message to the thread history."""
    r = get_redis()
    key = _key(channel, thread_ts)
    entry = json.dumps({"role": role, "content": content, "ts": time.time()})
    r.lpush(key, entry)
    r.ltrim(key, 0, MAX_MESSAGES - 1)
    r.expire(key, THREAD_TTL)


def get_history(channel: str, thread_ts: str) -> list[dict[str, Any]]:
    """Return messages in chronological order (oldest first)."""
    r = get_redis()
    raw = r.lrange(_key(channel, thread_ts), 0, -1)
    messages = []
    for m in raw:
        try:
            messages.append(json.loads(m))
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted message entry in thread memory")
    messages.reverse()   # LPUSH stores newest first
    return messages


def format_for_claude(channel: str, thread_ts: str) -> list[dict[str, str]]:
    """Return history as Claude messages array (role/content only)."""
    history = get_history(channel, thread_ts)
    return [{"role": m["role"], "content": m["content"]} for m in history]
