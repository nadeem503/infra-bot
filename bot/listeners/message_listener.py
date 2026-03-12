"""Message listener: handles @mentions.

All intelligence is powered by Claude AI:
  - ClaudeBrain.classify() understands any natural language command
  - ClaudeBrain.generate_response() writes casual, human-sounding replies

Flow on every @mention:
  1. Claude classifies intent: create_jira | assign_ticket | send_invite | infra_issue | unknown
  2. Bot executes the appropriate action
  3. Claude generates the Slack response
"""
import base64

import requests
from slack_bolt import App

from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from bot.nlp.claude_brain import brain
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_formatter = SlackFormatter()

JIRA_BASE = f"https://api.atlassian.com/ex/jira/{settings.JIRA_CLOUD_ID}/rest/api/3"
JIRA_BROWSE = "https://lambdatest.atlassian.net/browse"
PROJECT_KEY = "TE"
ISSUE_TYPE_ID = "10204"   # Simple Task
TEAM_FIELD = "b79a27b6-de36-4381-8d60-0b0c3e6477a7"

ISSUE_TO_ACTION: dict[str, str] = {
    "device_down": "device_status",
    "reboot": "ssh_reboot",
    "adb_issue": "adb_restart",
    "network_issue": "device_status",
    "db_mismatch": "db_query",
    "jenkins_failure": "jenkins_trigger",
    "app_crash": "adb_logcat",
    "storage_issue": "adb_clear_storage",
}


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------

def _jira_headers() -> dict:
    creds = base64.b64encode(
        f"{settings.JIRA_EMAIL}:{settings.JIRA_API_TOKEN}".encode()
    ).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------

def _exec_create_jira(params: dict) -> dict:
    """Create a Jira ticket. Returns result dict for Claude to format."""
    title = params.get("title", "").strip()
    assignee = params.get("assignee", "")
    cc: list[str] = params.get("cc", []) or []
    issue_type = params.get("issue_type", "Task")

    if not title:
        return {"success": False, "error": "Could not determine ticket title"}

    if not all([settings.JIRA_EMAIL, settings.JIRA_API_TOKEN]):
        return {"success": False, "error": "Jira credentials not configured"}

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
        return {"success": False, "error": type(exc).__name__}

    if resp.status_code != 201:
        return {"success": False, "error": data.get("errors", data.get("errorMessages", []))}

    key = data.get("key", "?")
    return {
        "success": True,
        "ticket_key": key,
        "url": f"{JIRA_BROWSE}/{key}",
        "title": title,
        "assignee": assignee,
        "cc": cc,
        "issue_type": issue_type,
    }


def _exec_assign_ticket(params: dict) -> dict:
    """Assign an existing Jira ticket."""
    ticket_key = params.get("ticket_key", "")
    assignee = params.get("assignee", "")
    cc: list[str] = params.get("cc", []) or []

    if not ticket_key or not assignee:
        return {"success": False, "error": "Need both ticket key and assignee"}

    try:
        resp = requests.put(
            f"{JIRA_BASE}/issue/{ticket_key}/assignee",
            json={"accountId": assignee},
            headers=_jira_headers(),
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": type(exc).__name__}

    if resp.status_code == 204:
        return {"success": True, "ticket_key": ticket_key, "assignee": assignee, "cc": cc}
    return {"success": False, "error": f"HTTP {resp.status_code}"}


# ---------------------------------------------------------------------------
# Infra issue → approval workflow
# ---------------------------------------------------------------------------

def _handle_infra_issue(
    params: dict,
    text: str,
    channel: str,
    thread_ts: str,
    user_id: str,
    say,  # noqa: ANN001
) -> None:
    issue_category = params.get("issue_category", "device_down")
    devices: list[str] = params.get("devices") or []
    region_slug: str = params.get("region") or "unknown"

    # Use formatter for region display
    from bot.analyzers.region_detector import RegionDetector  # noqa: PLC0415
    rd = RegionDetector()
    region_display = rd.get_display_name(region_slug if region_slug != "unknown" else None)

    action_type = ISSUE_TO_ACTION.get(issue_category, "device_status")

    action_params = {
        "devices": devices,
        "udid": devices[0] if devices else "",
        "host": devices[0] if devices else "",
        "query": "SELECT * FROM devices WHERE status = 'offline' LIMIT 10",
        "summary": f"[Infra-Bot] {issue_category} in {region_display}",
        "description": f"Detected via Slack: {text[:500]}",
    }

    action_id = approval_manager.create_action(
        action_type=action_type,
        params=action_params,
        channel=channel,
        thread_ts=thread_ts,
        requested_by=user_id,
        region=region_slug,
        devices=devices,
    )

    blocks = _formatter.format_analysis(
        issue_type=issue_category,
        region=region_slug,
        region_display=region_display,
        devices=devices,
        proposed_actions=[f"`{action_type}` for *{issue_category}*"],
        action_records=[{"action_id": action_id, "action_type": action_type}],
    )
    say(blocks=blocks, text=f"Infra-Bot: {issue_category}", thread_ts=thread_ts)
    logger.info("Infra issue posted: %s region=%s devices=%s", issue_category, region_slug, devices)


# ---------------------------------------------------------------------------
# Slack listener
# ---------------------------------------------------------------------------

def register_message_listeners(app: App) -> None:
    @app.event("app_mention")
    def handle_mention(event: dict, say, client) -> None:  # noqa: ANN001
        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user_id = event.get("user", "")

        logger.info("@mention from %s in %s", user_id, channel)

        # --- Claude classifies the message ---
        classification = brain.classify(text)
        intent = classification.get("intent", "unknown")
        params = classification.get("params", {})
        confidence = classification.get("confidence", 0.0)

        logger.info("Claude classified: intent=%s confidence=%.2f", intent, confidence)

        # --- Route by intent ---
        if intent == "create_jira":
            result = _exec_create_jira(params)
            response = brain.generate_response("created a Jira ticket", result)
            say(text=response, thread_ts=thread_ts)

        elif intent == "assign_ticket":
            result = _exec_assign_ticket(params)
            response = brain.generate_response("assigned a Jira ticket", result)
            say(text=response, thread_ts=thread_ts)

        elif intent == "send_invite":
            # Claude also generates the acknowledgement
            response = brain.generate_response("acknowledged a meeting invite request", params)
            say(text=response, thread_ts=thread_ts)

        elif intent == "infra_issue":
            _handle_infra_issue(params, text, channel, thread_ts, user_id, say)

        else:
            # Unknown — ask Claude for a friendly clarification
            response = brain.generate_response(
                "received an unclear message",
                {"original_text": text, "success": False},
            )
            say(text=response, thread_ts=thread_ts)
            logger.info("Unknown intent, Claude generated clarification")
