"""Action listener: handles Approve / Deny / Execute-Now button clicks.

Only APPROVER_SLACK_ID may approve actions.
Multi-device actions: live progress bar updated in-thread.
After completion:
  - Updates original approval card to show final status (audit trail)
  - Records outcome in learning store
  - Updates device personality tracker
  - Updates circuit breaker (failure/success counter)
  - Increments daily stats
Also handles: replay_action (from /infra history)
"""
from __future__ import annotations

import json
import threading
import time

from slack_bolt import App

from bot.actions.base_action import BaseAction
from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from bot.memory import circuit_breaker, device_tracker, learning_store
from bot.memory.redis_client import get_redis
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)
_formatter = SlackFormatter()

# Actions that require a second explicit confirmation before execution
DOUBLE_APPROVAL_ACTIONS: frozenset[str] = frozenset({"resigner_restart"})


def _get_action_handler(action_type: str) -> type[BaseAction] | None:
    from bot.actions.adb_action import ADBAction  # noqa: PLC0415
    from bot.actions.db_action import DBAction  # noqa: PLC0415
    from bot.actions.device_status import DeviceStatusAction  # noqa: PLC0415
    from bot.actions.device_disconnected_action import DeviceDisconnectedAction  # noqa: PLC0415
    from bot.actions.device_lifecycle_action import (  # noqa: PLC0415
        DeviceDisposeAction, DeviceHostUpdateAction,
    )
    from bot.actions.github_action import GitHubAction  # noqa: PLC0415
    from bot.actions.jenkins_action import JenkinsAction  # noqa: PLC0415
    from bot.actions.jira_action import JiraAction  # noqa: PLC0415
    from bot.actions.ssh_action import SSHAction  # noqa: PLC0415
    from bot.actions.macos_service_action import (  # noqa: PLC0415
        LRRRestartAction, ResignerRestartAction, IHMRestartAction,
        ReconcilerRestartAction, LRPRestartAction,
    )
    from bot.actions.ubuntu_service_action import (  # noqa: PLC0415
        RMDMRestartAction, RDTSARestartAction,
        AndroidContainerRestartAction, AllServicesStatusAction,
    )
    return {
        # Generic device actions
        "ssh_reboot":                SSHAction,
        "device_status":             DeviceStatusAction,
        "adb_restart":               ADBAction,
        "adb_logcat":                ADBAction,
        "adb_clear_storage":         ADBAction,
        "db_query":                  DBAction,
        "jenkins_trigger":           JenkinsAction,
        "github_workflow":           GitHubAction,
        "jira_ticket":               JiraAction,
        "device_disconnected":       DeviceDisconnectedAction,
        # macOS / iOS service actions
        "lrr_restart":               LRRRestartAction,
        "resigner_restart":          ResignerRestartAction,
        "ihm_restart":               IHMRestartAction,
        "reconciler_restart":        ReconcilerRestartAction,
        "lrp_restart":               LRPRestartAction,
        # Ubuntu / Android service actions
        "rmdm_restart":              RMDMRestartAction,
        "rdtsa_restart":             RDTSARestartAction,
        "android_container_restart": AndroidContainerRestartAction,
        "host_service_status":       AllServicesStatusAction,
        # Device lifecycle (GitHub Actions workflows)
        "device_dispose":            DeviceDisposeAction,
        "device_migrate":            DeviceHostUpdateAction,
    }.get(action_type)


def _record_completion(record, result: dict, host: str | None = None) -> None:
    """Post-action bookkeeping: learning, device tracker, circuit breaker, daily stats."""
    success = result.get("success", False)

    # Learning store — record outcome for this (issue_type→action_type, region) pair
    from bot.analyzers.issue_detector import IssueDetector  # noqa: PLC0415
    det = IssueDetector()
    issue_type = det.get_issue_from_action(record.action_type) or record.action_type
    learning_store.record_outcome(issue_type, record.region, record.action_type, success)

    # Device tracker
    for d in record.devices:
        device_tracker.record_action(d, record.action_type)

    # Circuit breaker
    if host:
        if success:
            circuit_breaker.record_success(host)
        else:
            circuit_breaker.record_failure(host)

    # Daily stats
    r = get_redis()
    date_str = time.strftime("%Y-%m-%d")
    status_field = "success" if success else "failed"
    r.hincrby(f"infra:stats:daily:{date_str}", status_field, 1)
    r.expire(f"infra:stats:daily:{date_str}", 8 * 86400)
    r.hincrby(f"infra:stats:daily:{date_str}:issues", record.action_type, 1)
    r.expire(f"infra:stats:daily:{date_str}:issues", 8 * 86400)


def _update_approval_card(record, result: dict, client, channel: str) -> None:
    """Edit the original approval card to show completed status (audit trail)."""
    if not record.approval_msg_ts:
        return
    success = result.get("success", False)
    icon = ":white_check_mark:" if success else ":x:"
    msg = result.get("message", "")[:120]
    try:
        client.chat_update(
            channel=channel,
            ts=record.approval_msg_ts,
            text=f"{icon} `{record.action_type}` completed",
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"~Action pending~ → {icon} `{record.action_type}` "
                        f"completed\n_{msg}_"
                    ),
                },
            }],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not update approval card: %s", exc)


def _run_single(record, user_id: str, client, channel: str, thread_ts: str) -> None:
    host = record.params.get("host") or (record.devices[0] if record.devices else None)

    # Circuit breaker check
    if host and circuit_breaker.is_tripped(host):
        ttl = circuit_breaker.trip_ttl(host)
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=(
                f":zap: *Circuit breaker tripped* for `{host}` — 3 consecutive failures. "
                f"Actions paused for ~{max(1, ttl // 60)}m. Manual investigation recommended."
            ),
        )
        approval_manager.complete(record.action_id, {"success": False, "message": "Circuit breaker active"})
        return

    ActionClass = _get_action_handler(record.action_type)
    if not ActionClass:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=_formatter.format_error(f"No handler for `{record.action_type}`"),
        )
        return

    action = ActionClass(params=record.params, triggered_by=user_id, channel=channel, region=record.region)
    try:
        result = action.run()
    except PermissionError as exc:
        result = {"success": False, "message": f":no_entry: Permission denied: {exc}", "details": {}}
    except Exception as exc:  # noqa: BLE001
        logger.error("Action execution error: %s", exc)
        result = {"success": False, "message": f"Execution error: {type(exc).__name__}", "details": {}}

    approval_manager.complete(record.action_id, result)
    _record_completion(record, result, host)
    _update_approval_card(record, result, client, channel)

    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=_formatter.format_result(record.action_type, result),
    )
    logger.info("Action %s done: success=%s", record.action_id, result.get("success"))


def _run_bulk(record, user_id: str, client, channel: str, thread_ts: str) -> None:
    ActionClass = _get_action_handler(record.action_type)
    if not ActionClass:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=_formatter.format_error(f"No handler for `{record.action_type}`"),
        )
        return

    devices = record.devices
    n = len(devices)
    header = f":arrows_counterclockwise: *Processing {n} devices — `{record.action_type}`:*"
    progress_lines = [
        f":white_square: `[{i+1}/{n}]` `{d}` \u2192 queued"
        for i, d in enumerate(devices)
    ]

    init_resp = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"{header}\n" + "\n".join(progress_lines),
    )
    progress_ts = init_resp["ts"]

    results = []
    for i, device in enumerate(devices):
        host = device
        if host and circuit_breaker.is_tripped(host):
            results.append({"success": False})
            ttl = circuit_breaker.trip_ttl(host)
            progress_lines[i] = f":zap: `[{i+1}/{n}]` `{device}` \u2192 circuit breaker tripped (~{max(1, ttl // 60)}m)"
            client.chat_update(channel=channel, ts=progress_ts, text=f"{header}\n" + "\n".join(progress_lines))
            continue

        progress_lines[i] = f":hourglass: `[{i+1}/{n}]` `{device}` \u2192 in progress..."
        client.chat_update(channel=channel, ts=progress_ts, text=f"{header}\n" + "\n".join(progress_lines))

        # Build per-device params:
        # - ssh_reboot: host=device (IP), keep existing udid from record.params
        # - jenkins_trigger: strip multi-device fields (host_ips, udids, host_udid_pairs)
        #   so _map_params_with_ai only sees the single current host and maps HOST_IP to it.
        #   Without this, the AI maps HOST_IP to the full list on every iteration.
        # - all others: udid=device is fine (device IS the UDID/serial for those actions)
        _MULTI_DEVICE_KEYS = frozenset({"host_ips", "udids", "host_udid_pairs"})
        if record.action_type == "ssh_reboot":
            device_params = {**record.params, "host": device, "devices": [device]}
        elif record.action_type == "jenkins_trigger":
            device_params = {k: v for k, v in record.params.items() if k not in _MULTI_DEVICE_KEYS}
            device_params.update({"udid": device, "host": device, "devices": [device]})
        else:
            device_params = {**record.params, "udid": device, "host": device, "devices": [device]}
        action = ActionClass(params=device_params, triggered_by=user_id, channel=channel, region=record.region)
        try:
            result = action.run()
            results.append(result)
            ok = ":white_check_mark:" if result.get("success") else ":x:"
            snippet = result.get("message", "done")[:60]
            progress_lines[i] = f"{ok} `[{i+1}/{n}]` `{device}` \u2192 {snippet}"
            if result.get("success"):
                circuit_breaker.record_success(host)
            else:
                circuit_breaker.record_failure(host)
        except Exception as exc:  # noqa: BLE001
            results.append({"success": False})
            progress_lines[i] = f":x: `[{i+1}/{n}]` `{device}` \u2192 {type(exc).__name__}"
            circuit_breaker.record_failure(host)

        client.chat_update(channel=channel, ts=progress_ts, text=f"{header}\n" + "\n".join(progress_lines))

    success_count = sum(1 for r in results if r.get("success"))
    final_result = {"success": success_count == n, "message": f"{success_count}/{n} devices succeeded"}
    final = f":white_check_mark: *Bulk complete: {success_count}/{n} succeeded*\n\n" + "\n".join(progress_lines)
    client.chat_update(channel=channel, ts=progress_ts, text=final)
    approval_manager.complete(record.action_id, final_result)
    _record_completion(record, final_result)
    _update_approval_card(record, final_result, client, channel)


def _execute_approved(record, user_id: str, client, channel: str, thread_ts: str) -> None:
    """Shared execution logic for approve / execute-now / reaction approval."""
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":hourglass: Executing `{record.action_type}` (approved by <@{user_id}>)...",
    )
    if len(record.devices) > 1:
        threading.Thread(
            target=_run_bulk,
            args=(record, user_id, client, channel, thread_ts),
            daemon=True,
        ).start()
    else:
        _run_single(record, user_id, client, channel, thread_ts)


def _get_pending_action_ids() -> set[str]:
    """Return action IDs that are currently in 'pending' status."""
    return {r.action_id for r in approval_manager.list_pending()}


def _is_double_approval_action(action_id: str) -> bool:
    record = approval_manager.get_action(action_id)
    return record is not None and record.action_type in DOUBLE_APPROVAL_ACTIONS


def _post_second_confirmation(record, channel: str, thread_ts: str, client) -> None:  # noqa: ANN001
    """Post a second Block Kit card asking for explicit confirmation of a sensitive action."""
    devices_str = ", ".join(f"`{d}`" for d in record.devices[:5]) or "_no devices listed_"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":warning: *Double-confirmation required*\n\n"
                    f"Action `{record.action_type}` has been approved once.\n"
                    f"This will *restart Resigner and unlock the keychain* on {devices_str}.\n\n"
                    f"*Are you sure you want to proceed?*"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u26a0\ufe0f Yes, restart Resigner"},
                    "style": "danger",
                    "action_id": "confirm_resigner_restart",
                    "value": record.action_id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Final confirmation"},
                        "text": {"type": "mrkdwn", "text": "This will restart Resigner *and* unlock the macOS keychain. Proceed?"},
                        "confirm": {"type": "plain_text", "text": "Yes, proceed"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u274c Cancel"},
                    "style": "primary",
                    "action_id": "deny_action",
                    "value": record.action_id,
                },
            ],
        },
    ]
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        blocks=blocks,
        text=f":warning: Second confirmation required for `{record.action_type}`",
    )
    logger.info("Second confirmation card posted for action %s (%s)", record.action_id, record.action_type)


def register_action_listeners(app: App) -> None:
    def _common_approve(ack, body, client, label: str) -> None:
        ack()
        user_id: str = body["user"]["id"]
        action_id: str = body["actions"][0]["value"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        # Claim exclusive processing — Socket Mode can deliver the same button-click
        # event to multiple active connections (e.g. after a restart).  The NX lock
        # ensures only one handler processes each approval; the rest are silently dropped
        # instead of spamming "not found or already processed" warnings.
        lock_key = f"infra:action:processing:{action_id}"
        try:
            claimed = get_redis().set(lock_key, "1", nx=True, ex=30)
        except Exception:  # noqa: BLE001
            claimed = True   # Redis unavailable — proceed optimistically
        if not claimed:
            logger.debug("Action %s already claimed by another handler — dropping", action_id)
            return

        logger.info("%s: action_id=%s user=%s", label, action_id, user_id)

        if user_id != settings.APPROVER_SLACK_ID:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=_formatter.format_unauthorized(user_id),
            )
            return

        # Double-approval actions get pre_approved first, then require a second confirm
        if action_id in _get_pending_action_ids() and _is_double_approval_action(action_id):
            record = approval_manager.pre_approve(action_id, user_id)
        else:
            record = approval_manager.approve(action_id, user_id)

        if not record:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":warning: Action `{action_id}` not found or already processed.",
            )
            return

        if record.status == "expired":
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=_formatter.format_expired(record.action_type),
            )
            return

        if record.status == "pre_approved":
            # Post second confirmation card
            _post_second_confirmation(record, channel, thread_ts, client)
            return

        _execute_approved(record, user_id, client, channel, thread_ts)

    @app.action("approve_action")
    def handle_approve(ack, body, client) -> None:  # noqa: ANN001
        _common_approve(ack, body, client, "Approve")

    @app.action("execute_now_action")
    def handle_execute_now(ack, body, client) -> None:  # noqa: ANN001
        _common_approve(ack, body, client, "ExecuteNow")

    @app.action("confirm_resigner_restart")
    def handle_confirm_resigner(ack, body, client) -> None:  # noqa: ANN001
        ack()
        user_id: str = body["user"]["id"]
        action_id: str = body["actions"][0]["value"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        logger.info("confirm_resigner_restart: action_id=%s user=%s", action_id, user_id)

        if user_id != settings.APPROVER_SLACK_ID:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=_formatter.format_unauthorized(user_id),
            )
            return

        record = approval_manager.confirm_approve(action_id, user_id)
        if not record:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":warning: Action `{action_id}` not found or not awaiting second confirmation.",
            )
            return

        _execute_approved(record, user_id, client, channel, thread_ts)

    @app.action("deny_action")
    def handle_deny(ack, body, client) -> None:  # noqa: ANN001
        ack()
        user_id: str = body["user"]["id"]
        action_id: str = body["actions"][0]["value"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        record = approval_manager.deny(action_id, user_id)
        if not record:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":warning: Action `{action_id}` not found or already processed.",
            )
            return

        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=_formatter.format_denied(record.action_type, user_id),
        )

    @app.action("replay_action")
    def handle_replay(ack, body, client) -> None:  # noqa: ANN001
        ack()
        user_id: str = body["user"]["id"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        replay_key: str = body["actions"][0]["value"]
        raw = get_redis().get(replay_key)
        if not raw:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":warning: Replay data expired — please run `/infra history` again.",
            )
            return

        data = json.loads(raw)
        action_type = data.get("action_type", "device_status")
        params = data.get("params", {})
        region = data.get("region", "unknown")
        devices = data.get("devices", [])

        from bot.formatters.slack_formatter import SlackFormatter  # noqa: PLC0415
        fmt = SlackFormatter()

        dry_run_preview: str | None = None
        ActionClass = _get_action_handler(action_type)
        if ActionClass:
            try:
                dry_run_preview = ActionClass(
                    params=params, triggered_by=user_id, channel=channel, region=region
                ).dry_run()
            except Exception:  # noqa: BLE001
                pass

        action_id = approval_manager.create_action(
            action_type=action_type,
            params=params,
            channel=channel,
            thread_ts=thread_ts,
            requested_by=user_id,
            region=region,
            devices=devices,
            dry_run_preview=dry_run_preview,
        )

        blocks = fmt.format_analysis(
            issue_type=action_type,
            region=region,
            region_display=region,
            devices=devices,
            proposed_actions=[f"`{action_type}` (replayed)"],
            action_records=[{"action_id": action_id, "action_type": action_type, "dry_run_preview": dry_run_preview}],
            personality_warnings=[],
            recommendation=None,
        )

        resp = client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            blocks=blocks,
            text=f":repeat: Replaying `{action_type}`",
        )
        if resp and resp.get("ts"):
            approval_manager.set_msg_ts(action_id, resp["ts"], channel)

        logger.info("Replay action created: %s (%s) by %s", action_id, action_type, user_id)
