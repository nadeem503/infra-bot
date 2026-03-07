"""Message listener: handles @mentions and drives the analysis pipeline.

Priority on every @mention:
  1. Natural language commands (create Jira, assign ticket, send invite) — execute immediately,
     respond with casual human-sounding confirmation.
  2. Infra issue detection — run analyzer pipeline and post approval buttons.
"""
import base64

import requests
from slack_bolt import App

from bot.analyzers.device_extractor import DeviceExtractor
from bot.analyzers.issue_detector import IssueDetector
from bot.analyzers.region_detector import RegionDetector
from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from bot.nlp.command_parser import CommandParser, ParsedCommand
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_issue_detector = IssueDetector()
_region_detector = RegionDetector()
_device_extractor = DeviceExtractor()
_formatter = SlackFormatter()
_command_parser = CommandParser()

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

JIRA_BASE = f"https://api.atlassian.com/ex/jira/{settings.JIRA_CLOUD_ID}/rest/api/3"
JIRA_BROWSE = "https://lambdatest.atlassian.net/browse"
PROJECT_KEY = "TE"
ISSUE_TYPE_ID = "10204"   # Simple Task
TEAM_FIELD = "b79a27b6-de36-4381-8d60-0b0c3e6477a7"


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------

def _jira_headers() -> dict:
    creds = base64.b64encode(
        f"{settings.JIRA_EMAIL}:{settings.JIRA_API_TOKEN}".encode()
    ).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# NLP command executors  —  casual, human-sounding replies
# ---------------------------------------------------------------------------

def _exec_create_jira(params: dict, thread_ts: str, say) -> None:  # noqa: ANN001
    title = params.get("title", "").strip()
    assignee = params.get("assignee", "")
    cc: list[str] = params.get("cc", [])
    issue_type = params.get("issue_type", "Task")

    if not title:
        say(text=":thinking_face: Couldn't figure out the ticket title — can you rephrase?",
            thread_ts=thread_ts)
        return

    if not all([settings.JIRA_EMAIL, settings.JIRA_API_TOKEN]):
        say(text=":x: Jira credentials not configured.", thread_ts=thread_ts)
        return

    payload: dict = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "summary": title,
            "issuetype": {"id": ISSUE_TYPE_ID},
            "customfield_10001": TEAM_FIELD,
        }
    }
    if assignee:
        payload["fields"]["assignee"] = {"id": assignee}

    try:
        resp = requests.post(f"{JIRA_BASE}/issue", json=payload,
                             headers=_jira_headers(), timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        say(text=f":x: Jira API error: {type(exc).__name__}", thread_ts=thread_ts)
        return

    if resp.status_code != 201:
        errors = data.get("errors", data.get("errorMessages", []))
        say(text=f":x: Couldn't create ticket: {errors}", thread_ts=thread_ts)
        return

    key = data.get("key", "?")
    url = f"{JIRA_BROWSE}/{key}"

    # Casual confirmation matching the observed style
    lines = [
        "Done :white_check_mark:",
        f"Created {url} \u2014 {title}",
    ]
    if assignee:
        lines.append(f"Assigned to <@{assignee}>")
    if cc:
        lines.append("cc: " + " ".join(f"<@{uid}>" for uid in cc))

    say(text="\n".join(lines), thread_ts=thread_ts)
    logger.info("Jira ticket created via NLP: %s", key)


def _exec_assign_ticket(params: dict, thread_ts: str, say) -> None:  # noqa: ANN001
    ticket_key = params.get("ticket_key", "")
    assignee = params.get("assignee", "")
    cc: list[str] = params.get("cc", [])

    if not ticket_key or not assignee:
        say(text=":thinking_face: Need both a ticket key and an assignee.", thread_ts=thread_ts)
        return

    try:
        resp = requests.put(
            f"{JIRA_BASE}/issue/{ticket_key}/assignee",
            json={"accountId": assignee},
            headers=_jira_headers(),
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        say(text=f":x: Error: {type(exc).__name__}", thread_ts=thread_ts)
        return

    if resp.status_code == 204:
        lines = [f"Done :white_check_mark:", f"*{ticket_key}* assigned to <@{assignee}>"]
        if cc:
            lines.append("cc: " + " ".join(f"<@{uid}>" for uid in cc))
        say(text="\n".join(lines), thread_ts=thread_ts)
    else:
        say(text=f":x: Couldn't assign {ticket_key}: HTTP {resp.status_code}",
            thread_ts=thread_ts)


def _exec_send_invite(params: dict, thread_ts: str, say) -> None:  # noqa: ANN001
    attendees: list[str] = params.get("attendees", [])
    freq = params.get("frequency", "")
    time_range = params.get("time_range", "")
    agenda = params.get("agenda", "")
    ensure = params.get("ensure", "")

    mentions = " ".join(f"<@{uid}>" for uid in attendees) if attendees else "the attendees"

    lines = [f"Got it :white_check_mark:"]
    lines.append(f"I'll send a recurring invite to {mentions}")
    if freq and time_range:
        lines.append(f"*Schedule:* every {freq}, {time_range} IST")
    elif freq:
        lines.append(f"*Schedule:* every {freq}")
    if agenda:
        lines.append(f"*Agenda:* {agenda}")
    if ensure:
        lines.append(f"*Note:* {ensure}")

    say(text="\n".join(lines), thread_ts=thread_ts)


def _handle_command(parsed: ParsedCommand, thread_ts: str, say) -> None:  # noqa: ANN001
    dispatch = {
        "create_jira": _exec_create_jira,
        "assign_ticket": _exec_assign_ticket,
        "send_invite": _exec_send_invite,
    }
    handler = dispatch.get(parsed.intent)
    if handler:
        handler(parsed.params, thread_ts, say)


# ---------------------------------------------------------------------------
# Slack listener registration
# ---------------------------------------------------------------------------

def register_message_listeners(app: App) -> None:
    @app.event("app_mention")
    def handle_mention(event: dict, say, client) -> None:  # noqa: ANN001
        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user_id = event.get("user", "")

        logger.info("@mention from %s in %s", user_id, channel)

        # --- Priority 1: NLP on-demand commands ---
        parsed = _command_parser.parse(text)
        if parsed:
            logger.info("NLP command detected: %s", parsed.intent)
            _handle_command(parsed, thread_ts, say)
            return

        # --- Priority 2: Infra issue detection + approval workflow ---
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
