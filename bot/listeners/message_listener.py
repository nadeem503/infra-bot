"""Message listener: handles @mentions and drives the analysis pipeline.

The bot activates ONLY when directly @mentioned — no passive channel scanning.
"""
from slack_bolt import App

from bot.analyzers.device_extractor import DeviceExtractor
from bot.analyzers.issue_detector import IssueDetector
from bot.analyzers.region_detector import RegionDetector
from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from utils.logger import get_logger

logger = get_logger(__name__)

_issue_detector = IssueDetector()
_region_detector = RegionDetector()
_device_extractor = DeviceExtractor()
_formatter = SlackFormatter()

ISSUE_TO_ACTION: dict[str, str] = {
    "reboot": "ssh_reboot",
    "device_down": "device_status",
    "db_mismatch": "db_query",
    "adb_issue": "adb_restart",
    "jenkins_failure": "jenkins_trigger",
    "network_issue": "device_status",
    "app_crash": "adb_logcat",
    "storage_issue": "adb_clear_storage",
}


def register_message_listeners(app: App) -> None:
    @app.event("app_mention")
    def handle_mention(event: dict, say) -> None:  # noqa: ANN001
        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user_id = event.get("user", "")

        logger.info("@mention from %s in %s", user_id, channel)

        # Analysis pipeline
        issues = _issue_detector.detect_all(text)
        region_slug = _region_detector.detect(text)
        region_display = _region_detector.get_display_name(region_slug)
        devices = [d.value for d in _device_extractor.extract(text)]

        primary_issue = issues[0]["category"] if issues else "general"
        proposed_actions: list[str] = []
        action_records: list[dict] = []

        if issues:
            for issue in issues[:3]:
                action_type = ISSUE_TO_ACTION.get(issue["category"], "device_status")
                proposed_actions.append(
                    f"`{action_type}` for *{issue['category']}* (severity: {issue['severity']})"
                )
                params = {
                    "devices": devices,
                    "udid": devices[0] if devices else "",
                    "host": devices[0] if devices else "",
                    "query": "SELECT * FROM devices WHERE status = 'offline' LIMIT 10",
                    "summary": f"[Infra-Bot] {issue['category']} in {region_display}",
                    "description": f"Detected via Slack: {text[:500]}",
                }
                action_id = approval_manager.create_action(
                    action_type=action_type,
                    params=params,
                    channel=channel,
                    thread_ts=thread_ts,
                    requested_by=user_id,
                    region=region_slug or "unknown",
                    devices=devices,
                )
                action_records.append({"action_id": action_id, "action_type": action_type})
        else:
            proposed_actions.append("`device_status` — general health check")
            params = {"devices": devices, "udid": devices[0] if devices else "", "host": ""}
            action_id = approval_manager.create_action(
                action_type="device_status",
                params=params,
                channel=channel,
                thread_ts=thread_ts,
                requested_by=user_id,
                region=region_slug or "unknown",
                devices=devices,
            )
            action_records.append({"action_id": action_id, "action_type": "device_status"})

        blocks = _formatter.format_analysis(
            issue_type=primary_issue,
            region=region_slug,
            region_display=region_display,
            devices=devices,
            proposed_actions=proposed_actions,
            action_records=action_records,
        )
        say(blocks=blocks, text=f"Infra-Bot: {primary_issue}", thread_ts=thread_ts)

        logger.info(
            "Analysis posted: issue=%s region=%s devices=%d actions=%d",
            primary_issue, region_slug, len(devices), len(action_records),
        )
