"""Redis-backed store for thread monitor jobs.

A monitor job tells the bot to ping a target user in a specific Slack
thread at a fixed interval until the job owner cancels it.

Redis key layout:
  monitor:job:{job_id}   — hash with all job fields
  monitor:index          — set of all active job_ids (for fast scan)
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_JOB_TTL = 86_400 * 3   # auto-expire jobs after 3 days (safety net)
_INDEX_KEY = "monitor:index"


def _job_key(job_id: str) -> str:
    return f"monitor:job:{job_id}"


def create_monitor(
    *,
    channel: str,
    thread_ts: str,
    started_by: str,
    target_user_id: str,
    target_name: str,
    interval_seconds: int,
    ping_message: str,
) -> str:
    """Create and persist a new monitor job. Returns the job_id."""
    from bot.memory.redis_client import get_redis  # noqa: PLC0415

    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    job = {
        "job_id":           job_id,
        "channel":          channel,
        "thread_ts":        thread_ts,
        "started_by":       started_by,
        "target_user_id":   target_user_id,
        "target_name":      target_name,
        "interval_seconds": interval_seconds,
        "ping_message":     ping_message,
        "next_fire_at":     now + interval_seconds,   # first ping after one interval
        "created_at":       now,
        "active":           "1",
    }
    r = get_redis()
    r.hset(_job_key(job_id), mapping=job)
    r.expire(_job_key(job_id), _JOB_TTL)
    r.sadd(_INDEX_KEY, job_id)
    logger.info("monitor_store: created job %s (interval=%ds, target=%s)", job_id, interval_seconds, target_user_id)
    return job_id


def get_monitor(job_id: str) -> Optional[dict]:
    from bot.memory.redis_client import get_redis  # noqa: PLC0415
    data = get_redis().hgetall(_job_key(job_id))
    return data if data else None


def list_active_monitors() -> list[dict]:
    """Return all active monitor jobs."""
    from bot.memory.redis_client import get_redis  # noqa: PLC0415
    r = get_redis()
    job_ids = r.smembers(_INDEX_KEY)
    jobs = []
    for jid in job_ids:
        data = r.hgetall(_job_key(jid))
        if data and data.get("active") == "1":
            jobs.append(data)
        elif not data:
            # Key expired — clean up index
            r.srem(_INDEX_KEY, jid)
    return jobs


def cancel_monitor(job_id: str) -> bool:
    """Mark a job inactive. Returns True if found."""
    from bot.memory.redis_client import get_redis  # noqa: PLC0415
    r = get_redis()
    if not r.exists(_job_key(job_id)):
        return False
    r.hset(_job_key(job_id), "active", "0")
    r.srem(_INDEX_KEY, job_id)
    logger.info("monitor_store: cancelled job %s", job_id)
    return True


def cancel_monitors_for_thread(channel: str, thread_ts: str) -> int:
    """Cancel all active monitors on a given thread. Returns count cancelled."""
    cancelled = 0
    for job in list_active_monitors():
        if job["channel"] == channel and job["thread_ts"] == thread_ts:
            cancel_monitor(job["job_id"])
            cancelled += 1
    return cancelled


def cancel_monitors_by_user(started_by: str, channel: str, thread_ts: str) -> int:
    """Cancel all monitors started by a user in a specific thread."""
    cancelled = 0
    for job in list_active_monitors():
        if (job["started_by"] == started_by
                and job["channel"] == channel
                and job["thread_ts"] == thread_ts):
            cancel_monitor(job["job_id"])
            cancelled += 1
    return cancelled


def update_next_fire(job_id: str, next_fire_at: float) -> None:
    from bot.memory.redis_client import get_redis  # noqa: PLC0415
    get_redis().hset(_job_key(job_id), "next_fire_at", next_fire_at)
