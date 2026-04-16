"""Jenkins build status poller — background thread.

Checks all pending (triggered, unnotified) builds every 5 minutes.
When a build finishes (SUCCESS / FAILURE / ABORTED), posts the result
back to the original Slack thread and marks the build as notified.
"""
from __future__ import annotations

import threading
import time

from utils.jenkins_monitor import get_pending_builds, get_build_status, mark_notified
from utils.logger import get_logger

logger = get_logger(__name__)

_POLL_INTERVAL = 300   # 5 minutes
_MAX_BUILD_AGE = 7200  # don't poll builds older than 2 hours


_RESULT_ICONS = {
    "SUCCESS":  ":white_check_mark:",
    "FAILURE":  ":x:",
    "ABORTED":  ":no_entry:",
    "UNSTABLE": ":warning:",
}


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"


def _post_build_result(slack_client, build: dict, status: dict) -> None:
    result   = status.get("result", "UNKNOWN")
    icon     = _RESULT_ICONS.get(result, ":grey_question:")
    duration = _format_duration(status.get("duration_s", 0))
    job_name = build["job_name"]
    build_num = build["build_num"]
    build_url = build["build_url"]
    triggered_by = build.get("triggered_by", "")

    mention = f"<@{triggered_by}> " if triggered_by else ""
    msg = (
        f"{icon} {mention}Jenkins build finished\n"
        f":jenkins: *<{build_url}|{job_name} #{build_num}>*\n"
        f"• *Result:* `{result}`\n"
        f"• *Duration:* {duration}"
    )
    try:
        slack_client.chat_postMessage(
            channel=build["channel"],
            thread_ts=build["thread_ts"],
            text=msg,
        )
        logger.info("Posted build result: %s #%s → %s", job_name, build_num, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to post build result for %s #%s: %s", job_name, build_num, exc)


def _poll_once(slack_client) -> None:  # noqa: ANN001
    builds = get_pending_builds()
    if not builds:
        return

    now = time.time()
    for build in builds:
        job_name  = build.get("job_name", "")
        build_num = build.get("build_num")
        age       = now - build.get("triggered_at", now)

        if age > _MAX_BUILD_AGE:
            logger.info("Build %s #%s too old (%ds) — marking notified", job_name, build_num, age)
            mark_notified(job_name, build_num)
            continue

        status = get_build_status(job_name, build_num)
        if status is None:
            logger.debug("Build %s #%s — Jenkins unreachable, will retry", job_name, build_num)
            continue

        if status.get("building"):
            logger.debug("Build %s #%s still running", job_name, build_num)
            continue

        # Build finished
        _post_build_result(slack_client, build, status)
        mark_notified(job_name, build_num)


def start_poller(slack_client) -> None:  # noqa: ANN001
    """Start the background poller thread. Called once from main.py."""

    def _loop() -> None:
        logger.info("Jenkins poller started (interval=%ds)", _POLL_INTERVAL)
        while True:
            try:
                _poll_once(slack_client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Jenkins poller error: %s", exc)
            time.sleep(_POLL_INTERVAL)

    t = threading.Thread(target=_loop, name="jenkins-poller", daemon=True)
    t.start()
    logger.info("Jenkins poller thread started")
