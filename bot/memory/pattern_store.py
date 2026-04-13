"""Manual fix-pattern store: operator-noted resolution patterns in Redis.

When an operator tells Infra-Bot "note the pattern" or "this device is fixed,
remember this", the extracted pattern (issue description + fix steps) is
stored here keyed by (issue_type, region).

Future approval cards for the same issue_type surface the pattern so the
approver can instantly see "an operator solved this before with these steps".

Keys:
  infra:pattern:{issue_type}:{region}  → Redis list of JSON-serialised PatternEntry
                                          (lpush so index 0 is always most recent)
  infra:pattern:index                  → Redis set of all pattern list keys
"""
from __future__ import annotations

import json
import time

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

_PREFIX = "infra:pattern"
_INDEX_KEY = "infra:pattern:index"
_RETENTION_SECONDS = 365 * 86400   # keep patterns for 1 year


def save_pattern(
    udid: str,
    host: str,
    issue_type: str,
    pattern: str,
    steps: list[str],
    region: str = "unknown",
    saved_by: str = "",
    device_name: str = "",
) -> str:
    """Persist a fix pattern noted by an operator.

    Returns a pattern_id string so callers can reference it in logs.
    """
    r = get_redis()
    ts = int(time.time())
    pattern_id = f"{ts}_{udid[:8]}" if udid else str(ts)
    key = f"{_PREFIX}:{issue_type}:{region}"
    entry = {
        "pattern_id":  pattern_id,
        "udid":        udid,
        "host":        host,
        "device_name": device_name,
        "issue_type":  issue_type,
        "pattern":     pattern,
        "steps":       steps,
        "region":      region,
        "saved_by":    saved_by,
        "saved_at":    ts,
    }
    r.lpush(key, json.dumps(entry))
    r.expire(key, _RETENTION_SECONDS)
    r.sadd(_INDEX_KEY, key)
    r.expire(_INDEX_KEY, _RETENTION_SECONDS)
    logger.info("Pattern saved: pattern_id=%s issue_type=%s region=%s by=%s",
                pattern_id, issue_type, region, saved_by)
    return pattern_id


def get_patterns(issue_type: str, region: str = "unknown", limit: int = 3) -> list[dict]:
    """Return the most recent fix patterns for this issue_type + region.

    Falls back to 'unknown' region if the specific region has no entries yet,
    so patterns noted without a region context are still surfaced everywhere.
    """
    r = get_redis()
    results: list[dict] = []
    for reg in (region, "unknown"):
        key = f"{_PREFIX}:{issue_type}:{reg}"
        if reg == "unknown" and reg == region:
            # Don't double-query if region IS unknown
            break
        raw_list = r.lrange(key, 0, limit - 1)
        for raw in raw_list:
            try:
                results.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning("Corrupted pattern entry in %s — skipping", key)
        if results:
            break
    return results[:limit]


def list_all_patterns(limit: int = 50) -> list[dict]:
    """Return all stored patterns sorted by most-recently saved (for admin listing)."""
    r = get_redis()
    all_patterns: list[dict] = []
    for key in r.smembers(_INDEX_KEY):
        raw_list = r.lrange(key, 0, 0)   # just the latest entry per key
        for raw in raw_list:
            try:
                all_patterns.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return sorted(all_patterns, key=lambda x: x.get("saved_at", 0), reverse=True)[:limit]
