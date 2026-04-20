"""Thread monitor runner — background daemon thread.

Wakes every 30 seconds and fires any due monitor jobs persisted in Redis.
Because jobs are stored in Redis, they survive bot restarts — the runner
picks them back up on the next startup and resumes pinging on schedule.

Usage (called from main.py):
    from bot.workers.monitor_runner import start_monitor_runner
    start_monitor_runner(slack_client)
"""
from __future__ import annotations

import threading
import time

from utils.logger import get_logger

logger = get_logger(__name__)

_TICK_INTERVAL = 30   # check Redis every 30 seconds


def _fire_ping(slack_client, job: dict) -> None:
    """Post a reminder ping into the monitored thread."""
    try:
        slack_client.chat_postMessage(
            channel=job["channel"],
            thread_ts=job["thread_ts"],
            text=job["ping_message"],
        )
        logger.info(
            "monitor_runner: pinged %s in thread %s (job %s)",
            job["target_user_id"], job["thread_ts"], job["job_id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("monitor_runner: failed to post ping for job %s: %s", job["job_id"], exc)


def _run_loop(slack_client) -> None:
    logger.info("monitor_runner: started (tick every %ds)", _TICK_INTERVAL)
    while True:
        try:
            from bot.memory.monitor_store import list_active_monitors, update_next_fire  # noqa: PLC0415
            now = time.time()
            jobs = list_active_monitors()
            for job in jobs:
                try:
                    next_fire = float(job.get("next_fire_at", 0))
                except (ValueError, TypeError):
                    continue

                if now >= next_fire:
                    _fire_ping(slack_client, job)
                    interval = int(job.get("interval_seconds", 300))
                    update_next_fire(job["job_id"], now + interval)

        except Exception as exc:  # noqa: BLE001
            logger.warning("monitor_runner: tick error (non-fatal): %s", exc)

        time.sleep(_TICK_INTERVAL)


def start_monitor_runner(slack_client) -> threading.Thread:
    """Start the monitor runner as a daemon thread. Returns the thread."""
    t = threading.Thread(
        target=_run_loop,
        args=(slack_client,),
        name="monitor-runner",
        daemon=True,
    )
    t.start()
    logger.info("monitor_runner: daemon thread started")
    return t
