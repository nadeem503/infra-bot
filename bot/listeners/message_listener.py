"""Message listener: handles @mentions.

All intelligence is powered by Claude AI:
  - ClaudeBrain.classify() understands any natural language command (with thread context)
  - ClaudeBrain.generate_response() writes casual, human-sounding replies

Flow on every @mention:
  1. Load thread history from Redis
  2. Store user message in thread memory
  3. Claude classifies intent (with prior context for follow-ups)
  4. Dedup check for infra issues
  5. Route, execute, store bot reply in thread
"""
import base64

import requests
from slack_bolt import App

from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from bot.memory import thread_memory, dedup_store
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


def _get_action_class(action_type: str):
    """Return action class for dry-run preview generation."""
    from bot.actions.adb_action import ADBAction  # noqa: PLC0415
    from bot.actions.db_action import DBAction  # noqa: PLC0415
    from bot.actions.device_status import DeviceStatusAction  # noqa: PLC0415
    from bot.actions.jenkins_action import JenkinsAction  # noqa: PLC0415
    from bot.actions.ssh_action import SSHAction  # noqa: PLC0415
    mapping = {
        "ssh_reboot": SSHAction,
        "device_status": DeviceStatusAction,
        "adb_restart": ADBAction,
        "adb_logcat": ADBAction,
        "adb_clear_storage": ADBAction,
        "db_query": DBAction,
        "jenkins_trigger": JenkinsAction,
    }
    return mapping.get(action_type)


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
        "success": True, "ticket_key": key, "url": f"{JIRA_BROWSE}/{key}",
        "title": title, "assignee": assignee, "cc": cc, "issue_type": issue_type,
    }


def _exec_assign_ticket(params: dict) -> dict:
    ticket_key = params.get("ticket_key", "")
    assignee = params.get("assignee", "")
    cc: list[str] = params.get("cc", []) or []

    if not ticket_key or not assignee:
        return {"success": False, "error": "Need both ticket key and assignee"}
    try:
        resp = requests.put(
            f"{JIRA_BASE}/issue/{ticket_key}/assignee",
            json={"accountId": assignee},
            headers=_jira_headers(), timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": type(exc).__name__}

    if resp.status_code == 204:
        return {"success": True, "ticket_key": ticket_key, "assignee": assignee, "cc": cc}
    return {"success": False, "error": f"HTTP {resp.status_code}"}


# ---------------------------------------------------------------------------
# Infra issue -> approval workflow
# ---------------------------------------------------------------------------

def _handle_infra_issue(
    params: dict,
    text: str,
    channel: str,
    thread_ts: str,
    user_id: str,
    say,  # noqa: ANN001
) -> str:
    """Returns a short status string for thread memory."""
    issue_category = params.get("issue_category", "device_down")
    devices: list[str] = params.get("devices") or []
    region_slug: str = params.get("region") or "unknown"

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

    # --- Deduplication check ---
    first_device = devices[0] if devices else ""
    if first_device:
        existing = dedup_store.is_duplicate(first_device, issue_category)
        if existing:
            mins_left = max(1, dedup_store.ttl_remaining(first_device, issue_category) // 60)
            say(
                text=(
                    f":repeat: Already tracking *{issue_category}* for `{first_device}` — "
                    f"action pending approval :hourglass: (~{mins_left}m cooldown remaining)"
                ),
                thread_ts=thread_ts,
            )
            return f"Duplicate {issue_category} skipped"

    # --- Dry-run preview ---
    dry_run_preview: str | None = None
    ActionClass = _get_action_class(action_type)
    if ActionClass:
        try:
            temp = ActionClass(
                params=action_params, triggered_by=user_id,
                channel=channel, region=region_slug,
            )
            dry_run_preview = temp.dry_run()
        except Exception as exc:  # noqa: BLE001
            logger.warning("dry_run() failed: %s", exc)

    # --- Create approval record ---
    action_id = approval_manager.create_action(
        action_type=action_type,
        params=action_params,
        channel=channel,
        thread_ts=thread_ts,
        requested_by=user_id,
        region=region_slug,
        devices=devices,
        dry_run_preview=dry_run_preview,
    )

    # --- Mark dedup key with real action_id ---
    if first_device:
        dedup_store.mark_tracked(first_device, issue_category, action_id, channel, thread_ts)

    blocks = _formatter.format_analysis(
        issue_type=issue_category,
        region=region_slug,
        region_display=region_display,
        devices=devices,
        proposed_actions=[f"`{action_type}` for *{issue_category}*"],
        action_records=[{
            "action_id": action_id,
            "action_type": action_type,
            "dry_run_preview": dry_run_preview,
        }],
    )
    say(blocks=blocks, text=f"Infra-Bot: {issue_category}", thread_ts=thread_ts)
    logger.info("Infra issue posted: %s region=%s devices=%s", issue_category, region_slug, devices)
    return f"Analyzing {issue_category} — approval required"


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

        # --- Thread memory: load history BEFORE adding current message ---
        thread_history = thread_memory.format_for_claude(channel, thread_ts)
        thread_memory.add_message(channel, thread_ts, "user", text)

        # --- Claude classifies (with thread context for follow-ups) ---
        classification = brain.classify(text, thread_history=thread_history or None)
        intent = classification.get("intent", "unknown")
        params = classification.get("params", {})
        confidence = classification.get("confidence", 0.0)

        logger.info("Claude classified: intent=%s confidence=%.2f", intent, confidence)

        bot_reply: str | None = None

        # --- Route by intent ---
        if intent == "create_jira":
            result = _exec_create_jira(params)
            bot_reply = brain.generate_response("created a Jira ticket", result)
            say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "assign_ticket":
            result = _exec_assign_ticket(params)
            bot_reply = brain.generate_response("assigned a Jira ticket", result)
            say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "send_invite":
            bot_reply = brain.generate_response("acknowledged a meeting invite request", params)
            say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "infra_issue":
            bot_reply = _handle_infra_issue(params, text, channel, thread_ts, user_id, say)

        else:
            bot_reply = brain.generate_response(
                "received an unclear message",
                {"original_text": text, "success": False},
            )
            say(text=bot_reply, thread_ts=thread_ts)
            logger.info("Unknown intent, Claude generated clarification")

        # --- Store bot reply in thread memory for follow-up context ---
        if bot_reply:
            thread_memory.add_message(channel, thread_ts, "assistant", bot_reply)
