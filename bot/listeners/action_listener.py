"""Action listener: handles Approve / Deny button clicks.

Only APPROVER_SLACK_ID may approve actions.
"""
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


def register_action_listeners(app: App) -> None:
    @app.action("approve_action")
    def handle_approve(ack, body, client) -> None:  # noqa: ANN001
        ack()
        user_id: str = body["user"]["id"]
        action_id: str = body["actions"][0]["value"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        logger.info("Approve: action_id=%s user=%s", action_id, user_id)

        # Authorization
        if user_id != settings.APPROVER_SLACK_ID:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=_formatter.format_unauthorized(user_id),
            )
            logger.warning("Unauthorized approval attempt by %s", user_id)
            return

        record = approval_manager.approve(action_id, user_id)
        if not record:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: Action `{action_id}` not found or already processed.",
            )
            return

        if record.status == "expired":
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=_formatter.format_expired(record.action_type),
            )
            return

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":hourglass: Executing `{record.action_type}` (approved by <@{user_id}>)...",
        )

        ActionClass = _get_action_handler(record.action_type)
        if not ActionClass:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=_formatter.format_error(f"No handler for `{record.action_type}`"),
            )
            return

        action = ActionClass(
            params=record.params,
            triggered_by=user_id,
            channel=channel,
            region=record.region,
        )
        try:
            result = action.run()
        except PermissionError as exc:
            result = {"success": False, "message": f":no_entry: Permission denied: {exc}", "details": {}}
        except Exception as exc:  # noqa: BLE001
            logger.error("Action execution error: %s", exc)
            result = {"success": False, "message": f"Execution error: {type(exc).__name__}", "details": {}}

        approval_manager.complete(action_id, result)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=_formatter.format_result(record.action_type, result),
        )
        logger.info("Action %s done: success=%s", action_id, result.get("success"))

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
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: Action `{action_id}` not found or already processed.",
            )
            return

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=_formatter.format_denied(record.action_type, user_id),
        )
