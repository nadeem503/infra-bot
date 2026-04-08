"""Device personality tracker: per-device action history for instability detection.

Keys:
  infra:device:{id}:actions  → sorted set (score=ts, member="action_type:ts")
  infra:device:{id}:stats    → hash {last_issue, last_ts}

Provides:
  - Replacement warning if rebooted > 3x in 7 days
  - Predictive flag if ADB restarted >= 2x in 3 hours
"""
from __future__ import annotations

import time

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

SEVEN_DAYS = 7 * 86400
THREE_HOURS = 3 * 3600
REBOOT_THRESHOLD = 3
ADB_INSTABILITY_THRESHOLD = 2


def record_action(device_id: str, action_type: str) -> None:
    if not device_id:
        return
    r = get_redis()
    ts = time.time()
    key = f"infra:device:{device_id}:actions"
    r.zadd(key, {f"{action_type}:{ts}": ts})
    r.zremrangebyscore(key, 0, ts - SEVEN_DAYS)
    r.expire(key, SEVEN_DAYS + 3600)
    skey = f"infra:device:{device_id}:stats"
    r.hset(skey, mapping={"last_issue": action_type, "last_ts": int(ts)})
    r.expire(skey, SEVEN_DAYS + 3600)


def _count_in_window(device_id: str, prefix: str, window: float) -> int:
    r = get_redis()
    cutoff = time.time() - window
    entries = r.zrangebyscore(f"infra:device:{device_id}:actions", cutoff, "+inf")
    return sum(1 for e in entries if e.startswith(prefix))


def reboot_count_7d(device_id: str) -> int:
    return _count_in_window(device_id, "ssh_reboot", SEVEN_DAYS)


def adb_count_3h(device_id: str) -> int:
    return _count_in_window(device_id, "adb_", THREE_HOURS)


def check_replacement_needed(device_id: str) -> str | None:
    """Return replacement warning if device rebooted too many times this week."""
    count = reboot_count_7d(device_id)
    if count > REBOOT_THRESHOLD:
        return (
            f":warning: `{device_id}` has been rebooted *{count}x this week* "
            f"— consider physical inspection or replacement"
        )
    return None


def check_instability(device_id: str) -> str | None:
    """Return predictive flag if ADB restarts suggest impending failure."""
    count = adb_count_3h(device_id)
    if count >= ADB_INSTABILITY_THRESHOLD:
        return (
            f":crystal_ball: `{device_id}` showing early signs of instability "
            f"({count} ADB restarts in 3h) — preemptive reboot recommended?"
        )
    return None


def get_summary(device_id: str) -> dict:
    r = get_redis()
    stats = r.hgetall(f"infra:device:{device_id}:stats") or {}
    return {
        "device_id": device_id,
        "last_issue": stats.get("last_issue", "none"),
        "last_ts": int(stats.get("last_ts", 0)),
        "reboot_count_7d": reboot_count_7d(device_id),
        "adb_count_3h": adb_count_3h(device_id),
    }
