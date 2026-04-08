"""Reaction listener: ✅ emoji reaction from approver auto-approves pending actions.

Maps: message_ts → action_id via Redis key set when approval card is posted.
Supported reactions: white_check_mark, heavy_check_mark, approved
"""
from __future__ import annotations

from slack_bolt import App

from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from bot.memory.redis_client import get_redis
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)
_formatter = SlackFormatter()

APPROVE_REACTIONS = {"white_check_mark", "heavy_check_mark", "approved"}


def lookup_action_id(channel: str, msg_ts: str) -> str | None:
    return get_redis().get(f"infra:approval:msgts:{channel}:{msg_ts}")


def register_reaction_listeners(app: App) -> None:
    @app.event("reaction_added")
    def handle_reaction(event: dict, client) -> None:
        # Only care about reactions on messages
        if event.get("item", {}).get("type") != "message":
            return

        emoji = event.get("reaction", "")
        if emoji not in APPROVE_REACTIONS:
            return

        user_id = event.get("user", "")
        if user_id != settings.APPROVER_SLACK_ID:
            return  # only approver's reactions count

        channel = event.get("item", {}).get("channel", "")
        msg_ts = event.get("item", {}).get("ts", "")
        if not channel or not msg_ts:
            return

        action_id = lookup_action_id(channel, msg_ts)
        if not action_id:
            logger.debug("Reaction :%s: on non-approval message ts=%s", emoji, msg_ts)
            return

        logger.info("Reaction approval: action_id=%s user=%s emoji=%s", action_id, user_id, emoji)

        record = approval_manager.approve(action_id, user_id)
        if not record:
            logger.warning("Reaction approval: action %s not found or not pending", action_id)
            return

        # Delegate to action executor (import here to avoid circular)
        from bot.listeners.action_listener import _execute_approved  # noqa: PLC0415
        _execute_approved(record, user_id, client, channel, record.thread_ts)
