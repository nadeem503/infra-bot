"""Slack Block Kit message formatter for infra-bot responses.

Supports multiple DC owners per region.
Auto-detects mention format:
  U... -> <@USERID>   (user mention)
  C... -> <#CHANNELID> (channel mention)
"""
import time
from typing import Optional

from utils.config_loader import get_dc_owners
from utils.logger import get_logger

logger = get_logger(__name__)


def _make_mention(slack_id: str) -> str:
    """Return the correct Slack mention syntax for a given ID."""
    if slack_id.startswith("C"):
        return f"<#{slack_id}>"   # channel mention
    return f"<@{slack_id}>"       # user mention


class SlackFormatter:
    def __init__(self) -> None:
        self._owners: Optional[dict] = None

    @property
    def owners(self) -> dict:
        if self._owners is None:
            self._owners = get_dc_owners()
        return self._owners

    def get_owner(self, region: Optional[str]) -> dict:
        return self.owners.get(region or "") or self.owners.get("default", {})

    def get_owner_mentions(self, region: Optional[str]) -> tuple[str, str]:
        """Return (mention_string, team_name) for a region."""
        owner = self.get_owner(region)
        name = owner.get("name", "Unknown")
        ids: list[str] = owner.get("slack_ids") or []
        if not ids and owner.get("slack_id"):
            ids = [owner["slack_id"]]
        mention = " ".join(_make_mention(sid) for sid in ids) if ids else name
        return mention, name

    def format_analysis(
        self,
        issue_type: Optional[str],
        region: Optional[str],
        region_display: str,
        devices: list[str],
        proposed_actions: list[str],
        action_records: list[dict],
    ) -> list[dict]:
        """Build Block Kit blocks for the main analysis response."""
        owner_mention, owner_name = self.get_owner_mentions(region)
        device_list = ", ".join(f"`{d}`" for d in devices) if devices else "_None identified_"
        actions_text = (
            "\n".join(f"\u2022 {a}" for a in proposed_actions)
            if proposed_actions
            else "\u2022 Device status check"
        )

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":rotating_light: *Infra AI Response*\n"
                        f"\u2022 *Issue Detected:* `{issue_type or 'general'}`\n"
                        f"\u2022 *Region:* {region_display}\n"
                        f"\u2022 *Devices:* {device_list}\n"
                        f"\u2022 *DC Owners:* {owner_mention} ({owner_name})\n"
                        f"\u2022 *Action Plan:*\n{actions_text}\n"
                        "\u2022 *Executing:* Awaiting approval :hourglass_flowing_sand:"
                    ),
                },
            },
            {"type": "divider"},
        ]

        for record in action_records:
            blocks.extend(self._approval_buttons(record))
        return blocks

    def _approval_buttons(self, record: dict) -> list[dict]:
        action_id = record["action_id"]
        action_type = record["action_type"]
        dry_run_preview = record.get("dry_run_preview")

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":wrench: *Proposed Action:* `{action_type}`"},
            },
        ]

        if dry_run_preview:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":mag: *Dry Run Preview:*\n```{dry_run_preview}```",
                },
            })

        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u2705 Approve"},
                    "style": "primary",
                    "action_id": "approve_action",
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\U0001f680 Execute Now"},
                    "style": "primary",
                    "action_id": "execute_now_action",
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u274c Deny"},
                    "style": "danger",
                    "action_id": "deny_action",
                    "value": action_id,
                },
            ],
        })
        return blocks

    def format_pending_list(self, records: list) -> str:
        """Format list of pending actions for /infra pending."""
        if not records:
            return ":white_check_mark: No pending actions right now."

        lines = [f":hourglass: *{len(records)} pending action(s):*\n"]
        for rec in records:
            age_min = max(0, int((time.time() - rec.requested_at) / 60))
            devices_str = ", ".join(f"`{d}`" for d in rec.devices[:3])
            if len(rec.devices) > 3:
                devices_str += f" +{len(rec.devices) - 3} more"
            if not devices_str:
                devices_str = "_no device_"
            lines.append(
                f"\u2022 `{rec.action_id}` \u2014 `{rec.action_type}` | "
                f"{rec.region} | {devices_str} | {age_min}m ago | <@{rec.requested_by}>"
            )
        return "\n".join(lines)

    def format_result(self, action_type: str, result: dict) -> str:
        icon = ":white_check_mark:" if result.get("success") else ":x:"
        message = result.get("message", "No message")
        details = result.get("details", {})
        text = f"{icon} *Action Completed:* `{action_type}`\n{message}"
        if details.get("output"):
            text += f"\n```{details['output'][:400]}```"
        if details.get("rows"):
            text += f"\n_Returned {len(details['rows'])} row(s)_"
        if details.get("url"):
            text += f"\n<{details['url']}|View ticket>"
        return text

    def format_denied(self, action_type: str, denier_id: str) -> str:
        return f":no_entry: Action `{action_type}` was denied by <@{denier_id}>"

    def format_expired(self, action_type: str) -> str:
        return f":timer_clock: Action `{action_type}` expired (30-minute TTL exceeded)"

    def format_unauthorized(self, user_id: str) -> str:
        return f":lock: <@{user_id}> is not authorized to approve actions"

    def format_error(self, message: str) -> str:
        return f":warning: *Error:* {message}"
