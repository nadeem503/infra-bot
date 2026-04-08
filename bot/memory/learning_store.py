"""Auto-learning store: tracks (issue_type, region, action_type) success rates.

After every completed action, success/failure is recorded in Redis.
On next similar issue, the approval card shows the recommendation:
  "Last 4 times device_down in AP was fixed with ssh_reboot (100% success)"
"""
from __future__ import annotations

import time

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

_PREFIX = "infra:learn"
RETENTION_DAYS = 90


def record_outcome(issue_type: str, region: str, action_type: str, success: bool) -> None:
    """Increment success/total counters for this pattern."""
    if not issue_type or not action_type:
        return
    r = get_redis()
    key = f"{_PREFIX}:{issue_type}:{region}:{action_type}"
    pipe = r.pipeline()
    pipe.hincrby(key, "total", 1)
    if success:
        pipe.hincrby(key, "success", 1)
    pipe.hset(key, "last_ts", int(time.time()))
    pipe.expire(key, RETENTION_DAYS * 86400)
    pipe.execute()
    logger.debug("Learning recorded: %s/%s/%s success=%s", issue_type, region, action_type, success)


def get_recommendation(issue_type: str, region: str) -> dict | None:
    """Return best action + stats for (issue_type, region), or None if insufficient data."""
    r = get_redis()
    best: dict | None = None
    best_rate = -1.0
    for key in r.scan_iter(f"{_PREFIX}:{issue_type}:{region}:*", count=50):
        data = r.hgetall(key)
        total = int(data.get("total", 0))
        if total < 2:
            continue
        success = int(data.get("success", 0))
        rate = success / total
        action_type = key.split(":")[-1]
        if rate > best_rate:
            best_rate = rate
            best = {
                "action_type": action_type,
                "success_rate": rate,
                "total": total,
                "success": success,
            }
    return best


def get_all_stats() -> list[dict]:
    """Return all learned patterns sorted by total runs (for home tab)."""
    r = get_redis()
    results = []
    for key in r.scan_iter(f"{_PREFIX}:*", count=200):
        parts = key.split(":")
        if len(parts) != 5:
            continue
        data = r.hgetall(key)
        total = int(data.get("total", 0))
        if total == 0:
            continue
        success = int(data.get("success", 0))
        results.append({
            "issue_type": parts[2],
            "region": parts[3],
            "action_type": parts[4],
            "success_rate": success / total,
            "total": total,
            "success": success,
        })
    return sorted(results, key=lambda x: x["total"], reverse=True)
