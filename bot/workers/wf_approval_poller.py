"""GitHub Actions workflow approval reminder poller.

Stores pending workflow runs in Redis. A background thread checks every 5 min.
If a run is still "waiting" (pending approval) after 1 hour, the mobile-infra
team is @mentioned again with a reminder. Reminder is sent once only.

Redis keys:
    infra:wf_pending          — Redis set of run keys
    infra:wf_run:{id}         — Hash: runs_url, channel, thread_ts, triggered_by,
                                 action_type, triggered_at, reminder_sent
"""
from __future__ import annotations

import re
import threading
import time

import requests

from config import settings
from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

_PENDING_SET   = "infra:wf_pending"
_RUN_KEY_PREFIX = "infra:wf_run"
_POLL_INTERVAL = 300        # check every 5 min
_REMINDER_AFTER = 3600      # remind after 1 hour of no approval
_RUN_TTL       = 86400      # keep run records for 24 h


def _run_key(run_id: str) -> str:
    return f"{_RUN_KEY_PREFIX}:{run_id}"


def _extract_run_id(runs_url: str) -> str:
    """Extract numeric run ID from a GitHub Actions run URL.

    e.g. https://github.com/org/repo/actions/runs/12345678 → "12345678"
    Falls back to the URL itself if no match.
    """
    m = re.search(r"/runs/(\d+)", runs_url or "")
    return m.group(1) if m else runs_url


def store_pending_run(
    runs_url: str,
    channel: str,
    thread_ts: str,
    triggered_by: str,
    action_type: str,
) -> None:
    """Store a newly triggered workflow run for approval monitoring."""
    if not runs_url:
        return
    run_id = _extract_run_id(runs_url)
    key = _run_key(run_id)
    try:
        r = get_redis()
        r.hset(key, mapping={
            "runs_url":     runs_url,
            "channel":      channel,
            "thread_ts":    thread_ts,
            "triggered_by": triggered_by,
            "action_type":  action_type,
            "triggered_at": str(time.time()),
            "reminder_sent": "0",
        })
        r.expire(key, _RUN_TTL)
        r.sadd(_PENDING_SET, run_id)
        logger.info("WF approval monitor: stored run %s (%s)", run_id, action_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WF store_pending_run failed: %s", exc)


def _get_run_status(run_id: str) -> str:
    """Query GitHub API for workflow run status.

    Returns: "waiting" | "in_progress" | "completed" | "unknown"
    """
    if not settings.GITHUB_TOKEN:
        return "unknown"
    # Try to get repo from a stored run URL
    try:
        r = get_redis()
        runs_url = r.hget(_run_key(run_id), "runs_url") or ""
        m = re.search(r"github\.com/([^/]+/[^/]+)/actions/runs/(\d+)", runs_url)
        if not m:
            return "unknown"
        repo, api_run_id = m.group(1), m.group(2)
        api_url = f"https://api.github.com/repos/{repo}/actions/runs/{api_run_id}"
        resp = requests.get(
            api_url,
            headers={
                "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "unknown")
            logger.debug("WF run %s status=%s", run_id, status)
            return status
    except Exception as exc:  # noqa: BLE001
        logger.warning("WF _get_run_status failed for %s: %s", run_id, exc)
    return "unknown"


def _slack_mention(slack_id: str) -> str:
    if slack_id.startswith("S"):
        return f"<!subteam^{slack_id}>"
    if slack_id.startswith("C"):
        return f"<#{slack_id}>"
    return f"<@{slack_id}>"


def _check_pending_runs(slack_client) -> None:  # noqa: ANN001
    """Check all pending runs. Send reminder if waiting > 1h and not yet reminded."""
    try:
        r = get_redis()
        run_ids = r.smembers(_PENDING_SET)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WF poller: Redis error: %s", exc)
        return

    now = time.time()
    for run_id in run_ids:
        try:
            key = _run_key(run_id)
            meta = r.hgetall(key)
            if not meta:
                r.srem(_PENDING_SET, run_id)
                continue

            reminder_sent = meta.get("reminder_sent", "0") == "1"
            triggered_at  = float(meta.get("triggered_at", now))
            elapsed       = now - triggered_at

            # Already reminded or too soon — skip
            if reminder_sent:
                # Check if completed — clean up
                status = _get_run_status(run_id)
                if status == "completed":
                    r.srem(_PENDING_SET, run_id)
                    logger.info("WF run %s completed — removed from monitor", run_id)
                continue

            if elapsed < _REMINDER_AFTER:
                continue

            # 1h passed — check if still waiting
            status = _get_run_status(run_id)
            if status == "completed":
                r.srem(_PENDING_SET, run_id)
                logger.info("WF run %s completed — no reminder needed", run_id)
                continue

            if status in ("waiting", "in_progress", "unknown"):
                # Send reminder
                runs_url      = meta.get("runs_url", "")
                channel       = meta.get("channel", "")
                thread_ts     = meta.get("thread_ts", "")
                triggered_by  = meta.get("triggered_by", "")
                action_type   = meta.get("action_type", "workflow")

                if not channel or not slack_client:
                    continue

                notify_id = settings.MOBILE_INFRA_SLACK_ID
                mention   = _slack_mention(notify_id) if notify_id else ""
                triggerer = f"<@{triggered_by}>" if triggered_by else "someone"
                label     = "dispose" if "dispose" in action_type else "migration"

                msg = (
                    f":bell: *Reminder — {label} workflow still awaiting approval* (1h elapsed)\n"
                    f"Triggered by {triggerer}\n"
                    + (f":link: <{runs_url}|Review & Approve Workflow Run>\n" if runs_url else "")
                    + (f"\n{mention} please approve when ready." if mention else "")
                )

                slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=msg,
                )
                r.hset(key, "reminder_sent", "1")
                logger.info("WF reminder sent for run %s in %s", run_id, channel)

        except Exception as exc:  # noqa: BLE001
            logger.warning("WF poller: error processing run %s: %s", run_id, exc)


def _poll_loop(slack_client) -> None:  # noqa: ANN001
    while True:
        time.sleep(_POLL_INTERVAL)
        try:
            _check_pending_runs(slack_client)
        except Exception as exc:  # noqa: BLE001
            logger.error("WF approval poller error: %s", exc)


def start_wf_approval_poller(slack_client) -> None:  # noqa: ANN001
    """Start background thread. Call once from main.py at startup."""
    t = threading.Thread(
        target=_poll_loop,
        args=(slack_client,),
        daemon=True,
        name="wf-approval-poller",
    )
    t.start()
    logger.info("WF approval poller started (checks every %ds, reminds after %ds)",
                _POLL_INTERVAL, _REMINDER_AFTER)
