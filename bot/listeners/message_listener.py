"""Message listener: handles @mentions.

Flow on every @mention:
  1. Rate limit check
  2. Load thread history (Redis) for follow-up context
  3. Store user message in thread memory
  4. Claude classifies intent + confidence
  5. If confidence < 0.6 → post clarification card (A/B/C buttons)
  6. Route by intent:
     - infra_issue → dedup check → device personality flags → dry-run → approval flow
                   → root cause signal tracking
     - create_jira / assign_ticket / send_invite → execute directly
  7. Store bot reply in thread memory
"""
from __future__ import annotations

import base64
import json
import uuid

import requests
from slack_bolt import App

from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from bot.memory import dedup_store, thread_memory
from bot.memory.rate_limiter import check_and_increment, set_last_action, ttl_remaining
from bot.memory.redis_client import get_redis
from bot.analyzers.root_cause_analyzer import (
    add_signal, already_analyzed, mark_analyzed, should_correlate, format_signals_for_claude,
)
from bot.nlp.claude_brain import brain
from config import settings
from utils.device_name import get_device_name
from utils.logger import get_logger

logger = get_logger(__name__)
_formatter = SlackFormatter()

JIRA_BASE = f"https://api.atlassian.com/ex/jira/{settings.JIRA_CLOUD_ID}/rest/api/3"
JIRA_BROWSE = "https://lambdatest.atlassian.net/browse"
PROJECT_KEY = "TE"
ISSUE_TYPE_ID = "10204"
TEAM_FIELD = "b79a27b6-de36-4381-8d60-0b0c3e6477a7"

CONFIDENCE_THRESHOLD = 0.6

ISSUE_TO_ACTION: dict[str, str] = {
    "device_down": "device_status",
    "reboot": "ssh_reboot",
    "adb_issue": "adb_restart",
    "network_issue": "device_status",
    "db_mismatch": "db_query",
    "jenkins_failure": "jenkins_trigger",
    "app_crash": "adb_logcat",
    "storage_issue": "adb_clear_storage",
    "device_disconnected": "device_disconnected",
}


def _get_action_class(action_type: str):
    from bot.actions.adb_action import ADBAction  # noqa: PLC0415
    from bot.actions.db_action import DBAction  # noqa: PLC0415
    from bot.actions.device_status import DeviceStatusAction  # noqa: PLC0415
    from bot.actions.jenkins_action import JenkinsAction  # noqa: PLC0415
    from bot.actions.ssh_action import SSHAction  # noqa: PLC0415
    from bot.actions.device_disconnected_action import DeviceDisconnectedAction  # noqa: PLC0415
    return {
        "ssh_reboot": SSHAction,
        "device_status": DeviceStatusAction,
        "adb_restart": ADBAction,
        "adb_logcat": ADBAction,
        "adb_clear_storage": ADBAction,
        "db_query": DBAction,
        "jenkins_trigger": JenkinsAction,
        "device_disconnected": DeviceDisconnectedAction,
    }.get(action_type)


def _jira_headers() -> dict:
    creds = base64.b64encode(
        f"{settings.JIRA_EMAIL}:{settings.JIRA_API_TOKEN}".encode()
    ).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


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
        resp = requests.post(f"{JIRA_BASE}/issue", json=payload, headers=_jira_headers(), timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": type(exc).__name__}
    if resp.status_code != 201:
        return {"success": False, "error": data.get("errors", data.get("errorMessages", []))}
    key = data.get("key", "?")
    return {"success": True, "ticket_key": key, "url": f"{JIRA_BROWSE}/{key}", "title": title, "assignee": assignee, "cc": cc, "issue_type": issue_type}


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


def _handle_infra_issue(
    params: dict,
    text: str,
    channel: str,
    thread_ts: str,
    user_id: str,
    say,
    client=None,
) -> str:
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

    # --- Rate limiting ---
    allowed, count = check_and_increment(user_id)
    if not allowed:
        ttl = ttl_remaining(user_id)
        say(
            text=(
                f":traffic_light: <@{user_id}> you've triggered {count} infra actions in 10 min — "
                f"slow down. Resets in ~{max(1, ttl // 60)}m"
            ),
            thread_ts=thread_ts,
        )
        return "Rate limited"

    # --- Deduplication ---
    first_device = devices[0] if devices else ""
    if first_device:
        existing = dedup_store.is_duplicate(first_device, issue_category)
        if existing:
            mins_left = max(1, dedup_store.ttl_remaining(first_device, issue_category) // 60)
            say(
                text=(
                    f":repeat: Already tracking *{issue_category}* for "
                    f"`{get_device_name(first_device)}` — action pending approval "
                    f":hourglass: (~{mins_left}m cooldown remaining)"
                ),
                thread_ts=thread_ts,
            )
            return f"Duplicate {issue_category} skipped"

    # --- Device personality warnings ---
    from bot.memory import device_tracker  # noqa: PLC0415
    personality_warnings: list[str] = []
    for d in devices[:3]:
        w = device_tracker.check_replacement_needed(d)
        if w:
            personality_warnings.append(w)
        p = device_tracker.check_instability(d)
        if p:
            personality_warnings.append(p)

    # --- Learning recommendation ---
    from bot.memory import learning_store  # noqa: PLC0415
    recommendation = learning_store.get_recommendation(issue_category, region_slug)

    # --- Dry-run preview ---
    dry_run_preview: str | None = None
    ActionClass = _get_action_class(action_type)
    if ActionClass:
        try:
            dry_run_preview = ActionClass(
                params=action_params, triggered_by=user_id,
                channel=channel, region=region_slug,
            ).dry_run()
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

    if first_device:
        dedup_store.mark_tracked(first_device, issue_category, action_id, channel, thread_ts)

    set_last_action(user_id, action_type)

    # --- Root cause signal tracking ---
    signals = add_signal(channel, issue_category, first_device, region_slug, user_id)
    if should_correlate(signals) and not already_analyzed(channel):
        mark_analyzed(channel)
        root_cause = brain.analyze_root_cause(format_signals_for_claude(signals))
        say(text=root_cause, thread_ts=thread_ts)

    # --- Post approval card ---
    device_labels = [get_device_name(d) for d in devices]
    blocks = _formatter.format_analysis(
        issue_type=issue_category,
        region=region_slug,
        region_display=region_display,
        devices=device_labels,
        proposed_actions=[f"`{action_type}` for *{issue_category}*"],
        action_records=[{
            "action_id": action_id,
            "action_type": action_type,
            "dry_run_preview": dry_run_preview,
        }],
        personality_warnings=personality_warnings,
        recommendation=recommendation,
    )

    resp = say(blocks=blocks, text=f"Infra-Bot: {issue_category}", thread_ts=thread_ts)

    # Store message ts for audit trail editing + reaction approval
    if resp and resp.get("ts") and client:
        approval_manager.set_msg_ts(action_id, resp["ts"], channel)

    # Start escalation watcher
    if client:
        approval_manager.start_escalation_watcher(action_id, channel, thread_ts, client)

    logger.info("Infra issue posted: %s region=%s devices=%s", issue_category, region_slug, devices)
    return f"Analyzing {issue_category} — approval required"


def register_message_listeners(app: App) -> None:
    @app.event("app_mention")
    def handle_mention(event: dict, say, client) -> None:  # noqa: ANN001
        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user_id = event.get("user", "")

        logger.info("@mention from %s in %s", user_id, channel)

        thread_history = thread_memory.format_for_claude(channel, thread_ts)
        thread_memory.add_message(channel, thread_ts, "user", text)

        classification = brain.classify(text, thread_history=thread_history or None)
        intent = classification.get("intent", "unknown")
        params = classification.get("params", {})
        confidence = classification.get("confidence", 0.0)

        logger.info("Classified: intent=%s confidence=%.2f", intent, confidence)

        # --- Confidence gating ---
        if confidence < CONFIDENCE_THRESHOLD and intent != "unknown":
            options = brain.clarification_options(text)
            if options:
                clarify_id = str(uuid.uuid4())[:8]
                get_redis().setex(
                    f"infra:clarify:{clarify_id}",
                    1800,
                    json.dumps({
                        "text": text, "options": options,
                        "channel": channel, "thread_ts": thread_ts, "user_id": user_id,
                    }),
                )
                blocks = _formatter.format_clarification_card(clarify_id, options)
                say(blocks=blocks, text="Not sure what you mean — pick one:", thread_ts=thread_ts)
                thread_memory.add_message(channel, thread_ts, "assistant", "Asked for clarification")
                return

        bot_reply: str | None = None

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
            bot_reply = _handle_infra_issue(params, text, channel, thread_ts, user_id, say, client)

        else:
            bot_reply = brain.generate_response(
                "received an unclear message",
                {"original_text": text, "success": False},
            )
            say(text=bot_reply, thread_ts=thread_ts)

        if bot_reply:
            thread_memory.add_message(channel, thread_ts, "assistant", bot_reply)

    @app.action("clarify_choice")
    def handle_clarify_choice(ack, body, client) -> None:  # noqa: ANN001
        ack()
        value: str = body["actions"][0]["value"]
        user_id: str = body["user"]["id"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        clarify_id, idx_str = value.rsplit(":", 1)
        idx = int(idx_str)

        raw = get_redis().get(f"infra:clarify:{clarify_id}")
        if not raw:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":warning: Clarification expired — please try again.",
            )
            return

        data = json.loads(raw)
        options = data.get("options", [])
        original_text = data.get("text", "")
        chosen = options[idx] if idx < len(options) else {}
        intent = chosen.get("intent", "unknown")
        params = chosen.get("params", {})

        logger.info("Clarify choice: clarify_id=%s idx=%d intent=%s", clarify_id, idx, intent)

        def _say(text=None, blocks=None, thread_ts=thread_ts, **kwargs):  # noqa: ANN001
            kw = {"channel": channel, "thread_ts": thread_ts}
            if blocks:
                client.chat_postMessage(**kw, blocks=blocks, text=text or "")
            elif text:
                client.chat_postMessage(**kw, text=text)

        if intent == "create_jira":
            result = _exec_create_jira(params)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=brain.generate_response("created a Jira ticket", result))
        elif intent == "assign_ticket":
            result = _exec_assign_ticket(params)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=brain.generate_response("assigned a Jira ticket", result))
        elif intent == "infra_issue":
            _handle_infra_issue(params, original_text, channel, thread_ts, user_id, _say, client)
        else:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":thinking_face: Got it — treating as: *{chosen.get('label', 'unknown')}*. Try rephrasing for a better result.",
            )
