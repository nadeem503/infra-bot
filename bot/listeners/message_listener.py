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
from bot.nlp.claude_brain import brain, _jira_created_reply, _jira_assigned_reply, _unclear_reply, _invite_reply
from bot.nlp.local_classifier import classify_local
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

# Simple greetings handled locally — no Gemini call needed
_GREETINGS = {"hello", "hi", "hey", "sup", "yo", "howdy", "hiya"}
_CAPABILITY_TRIGGERS = {"what can you do", "help", "commands", "capabilities", "what do you do"}

_GREETING_REPLY = (
    "Hey! :wave: I'm Infra-Bot — your DC infrastructure assistant.\n"
    "I can help with device issues, ADB restarts, reboots, Jira tickets, and more.\n"
    "Try: `@infra-bot device 10.151.x.x is down` or `@infra-bot create a jira task: ...`"
)
_CAPABILITIES_REPLY = (
    "*Here's what I can do:*\n\n"
    "*iOS / macOS hosts:*\n"
    "• :arrows_counterclockwise: Restart LRR (`lambda_remote_runner`) per device UDID\n"
    "• :lock: Restart Resigner + unlock keychain (health: port 6789)\n"
    "• :house: Restart IHM (iOS Host Manager)\n"
    "• :recycle: Restart LRP (Lambda Remote Provider)\n"
    "• :mag: Reconciler restart (macOS launchctl)\n\n"
    "*Android / Ubuntu hosts:*\n"
    "• :whale: Restart RMDM (Real Device Docker Manager)\n"
    "• :arrows_counterclockwise: Restart RDTSA (Traffic Service)\n"
    "• :package: Restart `adbd_<UDID>` Docker container\n"
    "• :recycle: Restart Reconciler (systemctl)\n\n"
    "*General:*\n"
    "• :satellite: Device down, ADB offline, reboot, network, DB mismatch\n"
    "• :ticket: Create & assign Jira tickets in project TE\n"
    "• :white_check_mark: Approval workflow with dry-run preview\n"
    "• :repeat: Dedup alerts (15-min cooldown), circuit breaker, rate limiting\n"
    "• :mag: Root cause analysis for correlated signals\n"
    "• :bar_chart: `/infra status|pending|history|faulty count`\n\n"
    "Just describe the problem: `@infra-bot LRR down on host 10.151.2.50`"
)

ISSUE_TO_ACTION: dict[str, str] = {
    # Generic device issues
    "device_down":              "device_status",
    "reboot":                   "ssh_reboot",
    "adb_issue":                "adb_restart",
    "network_issue":            "device_status",
    "db_mismatch":              "db_query",
    "jenkins_failure":          "jenkins_trigger",
    "app_crash":                "adb_logcat",
    "storage_issue":            "adb_clear_storage",
    "device_disconnected":      "device_disconnected",
    # macOS / iOS services
    "lrr_down":                 "lrr_restart",
    "resigner_down":            "resigner_restart",
    "ihm_down":                 "ihm_restart",
    "reconciler_down":          "reconciler_restart",
    "lrp_down":                 "lrp_restart",
    "cert_expired":             "resigner_restart",
    # Ubuntu / Android services
    "rmdm_down":                "rmdm_restart",
    "rdtsa_down":               "rdtsa_restart",
    "android_container_down":   "android_container_restart",
    # Generic host check
    "host_service_status":      "host_service_status",
}


def _get_action_class(action_type: str):
    from bot.actions.adb_action import ADBAction  # noqa: PLC0415
    from bot.actions.db_action import DBAction  # noqa: PLC0415
    from bot.actions.device_status import DeviceStatusAction  # noqa: PLC0415
    from bot.actions.jenkins_action import JenkinsAction  # noqa: PLC0415
    from bot.actions.ssh_action import SSHAction  # noqa: PLC0415
    from bot.actions.device_disconnected_action import DeviceDisconnectedAction  # noqa: PLC0415
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

        # Strip bot mention prefix for cleaner matching
        clean = text.split(">", 1)[-1].strip().lower().rstrip("?! ")

        # --- Local greeting / capability handling (no Gemini needed) ---
        if clean in _GREETINGS:
            say(text=_GREETING_REPLY, thread_ts=thread_ts)
            thread_memory.add_message(channel, thread_ts, "user", text)
            thread_memory.add_message(channel, thread_ts, "assistant", _GREETING_REPLY)
            return
        if any(trigger in clean for trigger in _CAPABILITY_TRIGGERS):
            say(text=_CAPABILITIES_REPLY, thread_ts=thread_ts)
            thread_memory.add_message(channel, thread_ts, "user", text)
            thread_memory.add_message(channel, thread_ts, "assistant", _CAPABILITIES_REPLY)
            return

        thread_history = thread_memory.format_for_claude(channel, thread_ts)
        thread_memory.add_message(channel, thread_ts, "user", text)

        # --- Try local classifier first (no Gemini call) ---
        classification = classify_local(text, thread_history or None)
        if classification:
            source = "local"
        else:
            # Local classifier couldn't handle it — fall back to Gemini
            classification = brain.classify(text, thread_history=thread_history or None)
            source = "gemini"

        intent = classification.get("intent", "unknown")
        params = classification.get("params", {})
        confidence = classification.get("confidence", 0.0)

        logger.info("Classified [%s]: intent=%s confidence=%.2f", source, intent, confidence)

        # --- Quota exceeded — surface friendly message instead of crashing ---
        if intent == "_quota_exceeded":
            from bot.nlp.claude_brain import _QUOTA_MSG
            say(text=_QUOTA_MSG, thread_ts=thread_ts)
            return

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
            bot_reply = _jira_created_reply(result)
            say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "assign_ticket":
            result = _exec_assign_ticket(params)
            bot_reply = _jira_assigned_reply(result)
            say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "send_invite":
            bot_reply = _invite_reply(params)
            say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "infra_issue":
            bot_reply = _handle_infra_issue(params, text, channel, thread_ts, user_id, say, client)

        else:
            bot_reply = _unclear_reply(text)
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
                                    text=_jira_created_reply(result))
        elif intent == "assign_ticket":
            result = _exec_assign_ticket(params)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=_jira_assigned_reply(result))
        elif intent == "infra_issue":
            _handle_infra_issue(params, original_text, channel, thread_ts, user_id, _say, client)
        else:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":thinking_face: Got it — treating as: *{chosen.get('label', 'unknown')}*. Try rephrasing for a better result.",
            )

    @app.event("message")
    def handle_message_events(body, logger) -> None:  # noqa: ANN001
        """No-op handler to silence Slack Bolt 404 warnings for message events."""
        pass
