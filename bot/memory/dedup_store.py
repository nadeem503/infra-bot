"""Duplicate issue deduplication backed by Redis.

Fingerprint: (device_id, issue_type) -> 15-minute cooldown.
If the same device + issue is reported again within the window,
the bot replies "Already tracking this" instead of creating a duplicate flow.
"""
from __future__ import annotations

import json
from typing import Optional

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

DEDUP_TTL = 900   # 15 minutes


def _key(device_id: str, issue_type: str) -> str:
    return f"infra:dedup:{device_id.replace('/', '_')}:{issue_type}"


def is_duplicate(device_id: str, issue_type: str) -> Optional[dict]:
    """Return existing record if this (device, issue) is already tracked, else None."""
    if not device_id:
        return None
    raw = get_redis().get(_key(device_id, issue_type))
    if raw:
        logger.info("Duplicate detected: device=%s issue=%s", device_id, issue_type)
        return json.loads(raw)
    return None


def mark_tracked(
    device_id: str,
    issue_type: str,
    action_id: str,
    channel: str,
    thread_ts: str,
) -> None:
    """Mark this (device, issue) as being tracked with 15-min TTL."""
    if not device_id:
        return
    record = {"action_id": action_id, "channel": channel, "thread_ts": thread_ts}
    get_redis().setex(_key(device_id, issue_type), DEDUP_TTL, json.dumps(record))


def clear(device_id: str, issue_type: str) -> None:
    """Remove dedup key when action completes."""
    get_redis().delete(_key(device_id, issue_type))


def ttl_remaining(device_id: str, issue_type: str) -> int:
    """Return seconds remaining in cooldown, negative if not set."""
    return get_redis().ttl(_key(device_id, issue_type))
