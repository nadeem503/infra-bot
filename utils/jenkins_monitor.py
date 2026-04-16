"""Jenkins build monitor — tracks triggered builds in Redis for background polling.

Stores build metadata keyed by job_name+build_num.
Background poller (bot/workers/jenkins_poller.py) reads from here every 5 min.
"""
from __future__ import annotations

import json
import time
from typing import Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "infra:jenkins:build:"
_INDEX_KEY  = "infra:jenkins:pending_builds"
_BUILD_TTL  = 7200   # 2 hours


def store_build(
    job_name: str,
    build_num: int,
    build_url: str,
    channel: str,
    thread_ts: str,
    triggered_by: str,
) -> None:
    """Store a triggered build for background status monitoring."""
    from bot.memory.redis_client import get_redis  # noqa: PLC0415
    r = get_redis()
    key = f"{_KEY_PREFIX}{job_name}:{build_num}"
    data = {
        "job_name": job_name,
        "build_num": build_num,
        "build_url": build_url.rstrip("/"),
        "channel": channel,
        "thread_ts": thread_ts,
        "triggered_by": triggered_by,
        "triggered_at": time.time(),
        "notified": False,
    }
    r.setex(key, _BUILD_TTL, json.dumps(data))
    r.sadd(_INDEX_KEY, key)
    r.expire(_INDEX_KEY, _BUILD_TTL)
    logger.info("Registered build for monitoring: %s #%s", job_name, build_num)


def get_pending_builds() -> list[dict]:
    """Return all unnotified builds stored for monitoring."""
    from bot.memory.redis_client import get_redis  # noqa: PLC0415
    r = get_redis()
    keys = r.smembers(_INDEX_KEY) or set()
    builds = []
    for key in keys:
        raw = r.get(key)
        if not raw:
            r.srem(_INDEX_KEY, key)  # stale key
            continue
        try:
            b = json.loads(raw)
            if not b.get("notified"):
                builds.append(b)
        except Exception:  # noqa: BLE001
            pass
    return builds


def mark_notified(job_name: str, build_num: int) -> None:
    """Mark a build as done so it won't be checked again."""
    from bot.memory.redis_client import get_redis  # noqa: PLC0415
    r = get_redis()
    key = f"{_KEY_PREFIX}{job_name}:{build_num}"
    raw = r.get(key)
    if raw:
        data = json.loads(raw)
        data["notified"] = True
        r.setex(key, 600, json.dumps(data))  # keep 10 min for on-demand queries then expire


def get_build_status(job_name: str, build_num: int) -> Optional[dict]:
    """Poll Jenkins REST API for current build status.

    Returns dict with keys: building, result, duration_s
    Returns None if Jenkins is unreachable or build not found.
    """
    if not settings.JENKINS_URL:
        return None
    url = f"{settings.JENKINS_URL.rstrip('/')}/job/{job_name}/{build_num}/api/json"
    try:
        resp = requests.get(
            url,
            auth=(settings.JENKINS_USER, settings.JENKINS_API_TOKEN),
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        duration_ms = data.get("duration") or data.get("estimatedDuration") or 0
        return {
            "building":   data.get("building", False),
            "result":     data.get("result"),       # SUCCESS / FAILURE / ABORTED / UNSTABLE / None
            "duration_s": duration_ms // 1000,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Build status check failed for %s #%s: %s", job_name, build_num, exc)
        return None


def get_recent_build_for_thread(channel: str, thread_ts: str) -> Optional[dict]:
    """Return the most recently triggered build for a given Slack thread, if unnotified."""
    builds = get_pending_builds()
    matches = [
        b for b in builds
        if b.get("channel") == channel and b.get("thread_ts") == thread_ts
    ]
    if not matches:
        # Also include notified builds (for on-demand status checks)
        from bot.memory.redis_client import get_redis  # noqa: PLC0415
        r = get_redis()
        keys = r.smembers(_INDEX_KEY) or set()
        for key in keys:
            raw = r.get(key)
            if raw:
                try:
                    b = json.loads(raw)
                    if b.get("channel") == channel and b.get("thread_ts") == thread_ts:
                        matches.append(b)
                except Exception:  # noqa: BLE001
                    pass
    if not matches:
        return None
    return max(matches, key=lambda b: b.get("triggered_at", 0))
