"""Action listener: handles Approve / Deny / Execute-Now button clicks.

Only APPROVER_SLACK_ID may approve actions.
For multi-device actions, posts a live progress bar in the thread.
"""
import threading

from slack_bolt import App

from bot.actions.base_action import BaseAction
from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)
_formatter = SlackFormatter()


def _get_action_handler(action_type: str) -> type[BaseAction] | None:
    from bot.actions.adb_action import ADBAction  # noqa: PLC0415
    from bot.actions.db_action import DBAction  # noqa: PLC0415
    from bot.actions.device_status import DeviceStatusAction  # noqa: PLC0415
    from bot.actions.github_action import GitHubAction  # noqa: PLC0415
    from bot.actions.jenkins_action import JenkinsAction  # noqa: PLC0415
    from bot.actions.jira_action import JiraAction  # noqa: PLC0415
    from bot.actions.ssh_action import SSHAction  # noqa: PLC0415

    mapping: dict[str, type[BaseAction]] = {
        "ssh_reboot": SSHAction,
        "device_status": DeviceStatusAction,
        "adb_restart": ADBAction,
        "adb_logcat": ADBAction,
        "adb_clear_storage": ADBAction,
        "db_query": DBAction,
        "jenkins_trigger": JenkinsAction,
        "github_workflow": GitHubAction,
        "jira_ticket": JiraAction,
    }
    return mapping.get(action_type)


def _run_single(record, user_id: str, client, channel: str, thread_ts: str) -> None:
    """Execute a single-device action."""
    ActionClass = _get_action_handler(record.action_type)
    if not ActionClass:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=_formatter.format_error(f"No handler for `{record.action_type}`"),
        )
        return

    action = ActionClass(
        params=record.params, triggered_by=user_id,
        channel=channel, region=record.region,
    )
    try:
        result = action.run()
    except PermissionError as exc:
        result = {"success": False, "message": f":no_entry: Permission denied: {exc}", "details": {}}
    except Exception as exc:  # noqa: BLE001
        logger.error("Action execution error: %s", exc)
        result = {"success": False, "message": f"Execution error: {type(exc).__name__}", "details": {}}

    approval_manager.complete(record.action_id, result)
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=_formatter.format_result(record.action_type, result),
    )
    logger.info("Action %s done: success=%s", record.action_id, result.get("success"))


def _run_bulk(record, user_id: str, client, channel: str, thread_ts: str) -> None:
    """Execute multi-device action with a live progress bar."""
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

    # Post initial progress message and keep its ts for updates
    init_resp = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"{header}\n" + "\n".join(progress_lines),
    )
    progress_ts = init_resp["ts"]

    results = []
    for i, device in enumerate(devices):
        progress_lines[i] = f":hourglass: `[{i+1}/{n}]` `{device}` \u2192 in progress..."
        client.chat_update(
            channel=channel, ts=progress_ts,
            text=f"{header}\n" + "\n".join(progress_lines),
        )

        device_params = {**record.params, "udid": device, "host": device, "devices": [device]}
        action = ActionClass(
            params=device_params, triggered_by=user_id,
            channel=channel, region=record.region,
        )
        try:
            result = action.run()
            results.append(result)
            ok = ":white_check_mark:" if result.get("success") else ":x:"
            snippet = result.get("message", "done")[:60]
            progress_lines[i] = f"{ok} `[{i+1}/{n}]` `{device}` \u2192 {snippet}"
        except Exception as exc:  # noqa: BLE001
            results.append({"success": False})
            progress_lines[i] = f":x: `[{i+1}/{n}]` `{device}` \u2192 {type(exc).__name__}"

        client.chat_update(
            channel=channel, ts=progress_ts,
            text=f"{header}\n" + "\n".join(progress_lines),
        )

    success_count = sum(1 for r in results if r.get("success"))
    final = (
        f":white_check_mark: *Bulk complete: {success_count}/{n} succeeded*\n\n"
        + "\n".join(progress_lines)
    )
    client.chat_update(channel=channel, ts=progress_ts, text=final)
    approval_manager.complete(
        record.action_id,
        {"success": success_count == n, "message": f"{success_count}/{n} devices succeeded"},
    )


def _execute_approved(record, user_id: str, client, channel: str, thread_ts: str) -> None:
    """Shared logic for approve_action and execute_now_action."""
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":hourglass: Executing `{record.action_type}` (approved by <@{user_id}>)...",
    )
    if len(record.devices) > 1:
        # Run bulk in a background thread so Slack's 3s ack window isn't exceeded
        threading.Thread(
            target=_run_bulk,
            args=(record, user_id, client, channel, thread_ts),
            daemon=True,
        ).start()
    else:
        _run_single(record, user_id, client, channel, thread_ts)


def register_action_listeners(app: App) -> None:
    def _common_approve(ack, body, client, label: str) -> None:
        ack()
        user_id: str = body["user"]["id"]
        action_id: str = body["actions"][0]["value"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        logger.info("%s: action_id=%s user=%s", label, action_id, user_id)

        if user_id != settings.APPROVER_SLACK_ID:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=_formatter.format_unauthorized(user_id),
            )
            logger.warning("Unauthorized %s attempt by %s", label, user_id)
            return

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

        _execute_approved(record, user_id, client, channel, thread_ts)

    @app.action("approve_action")
    def handle_approve(ack, body, client) -> None:  # noqa: ANN001
        _common_approve(ack, body, client, "Approve")

    @app.action("execute_now_action")
    def handle_execute_now(ack, body, client) -> None:  # noqa: ANN001
        _common_approve(ack, body, client, "ExecuteNow")

    @app.action("deny_action")
    def handle_deny(ack, body, client) -> None:  # noqa: ANN001
        ack()
        user_id: str = body["user"]["id"]
        action_id: str = body["actions"][0]["value"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        logger.info("Deny: action_id=%s user=%s", action_id, user_id)

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
