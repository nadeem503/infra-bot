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

import json
import re
import uuid

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
from utils.activity_log import log_user_request
from bot.actions.jira_client import (
    create_issue, assign_issue, transition_issue,
    resolve_slack_user_to_jira, check_ticket_completeness,
)
from config import settings
from utils.device_name import get_device_name
from utils.logger import get_logger

logger = get_logger(__name__)
_formatter = SlackFormatter()

JIRA_BROWSE = "https://lambdatest.atlassian.net/browse"

CONFIDENCE_THRESHOLD = 0.6

# Only these users may trigger infra actions (device checks, restarts, Jira, etc.)
# All other users receive greetings/capability replies only.
AUTHORIZED_USER_IDS: frozenset[str] = frozenset({
    "U04UTG30V9A",  # Nadeem Khan
    "U03GPJ43TJT",  # Pratik Parmar
    "U020L115A2X",  # Shivnarayan Shishodia
})

_UNAUTHORIZED_REPLY = (
    ":no_entry: Sorry, you're not authorized to trigger infra actions.\n"
    "Please contact <@U04UTG30V9A>, <@U03GPJ43TJT>, or <@U020L115A2X> for assistance."
)

def _clean_slack_text(text: str) -> str:
    """Strip Slack markdown so regex/Claude can parse device IDs reliably.

    Removes:  <@USERID>  <#CHANID|name>  <URL|label>  <URL>
    Decodes:  &amp; &lt; &gt; &nbsp;
    """
    # Strip bot/user/channel mentions: <@U...> <#C...|name>
    text = re.sub(r'<@[A-Z0-9]+>', '', text)
    text = re.sub(r'<#[A-Z0-9]+(?:\|[^>]+)?>', '', text)
    # Strip links: <https://...|label> → label,  <https://...> → ''
    text = re.sub(r'<https?://[^|>]*\|([^>]+)>', r'\1', text)
    text = re.sub(r'<https?://[^>]+>', '', text)
    # Decode HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ')
    return text.strip()


def _is_thin_text(s: str) -> bool:
    """True when text has no meaningful words/IPs/serials (e.g. just '👆' or whitespace)."""
    return not re.search(r'[a-zA-Z0-9]{3,}', s)


# Detects requests that need full thread context regardless of mention text length.
# e.g. "Summaries the thread", "tldr", "what happened", "explain above"
_CONTEXT_DEPENDENT_RE = re.compile(
    r'\b(summar(ize|ise|y|ies)|tldr|tl;dr|what.{0,15}(happened|going on|is this)|'
    r'explain\b|above|this thread|describe this|recap|overview)\b',
    re.IGNORECASE,
)


def _extract_message_text(msg: dict) -> str:
    """Extract all readable text from a Slack message including attachments and blocks.

    AlertBot posts device info (UDID, Host IP, Status, Remark) in attachment fields,
    not in the plain text field. Without this, thread context misses all device details.
    """
    parts = [msg.get("text", "").strip()]

    # Attachments: each can have .text + .fields[].title/.value
    for att in msg.get("attachments", []):
        if att.get("text"):
            parts.append(att["text"].strip())
        if att.get("pretext"):
            parts.append(att["pretext"].strip())
        for field in att.get("fields", []):
            title = field.get("title", "")
            value = field.get("value", "")
            if title and value:
                parts.append(f"{title}: {value}")
            elif value:
                parts.append(value)

    # Blocks: extract mrkdwn/plain_text section text
    for block in msg.get("blocks", []):
        text_obj = block.get("text", {})
        if isinstance(text_obj, dict) and text_obj.get("text"):
            parts.append(text_obj["text"].strip())
        for field in block.get("fields", []):
            if isinstance(field, dict) and field.get("text"):
                parts.append(field["text"].strip())

    return "\n".join(p for p in parts if p)


def _build_live_thread_history(client, channel: str, thread_ts: str, current_ts: str, limit: int = 10) -> list[dict]:
    """Fetch live Slack thread and return as Claude thread_history format.

    Captures ALL messages (including non-@mention user messages) so corrections
    like "use idevice_id not docker" posted without @mention are visible to Claude.
    Returns list of {"role": "user"|"assistant", "content": str} dicts.
    """
    try:
        replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        messages = replies.get("messages", [])
        # Exclude the current message itself
        messages = [m for m in messages if m.get("ts") != current_ts]
        # Take last N messages (most recent context)
        messages = messages[-limit:]
        history = []
        for m in messages:
            txt = _extract_message_text(m).strip()
            if not txt:
                continue
            role = "assistant" if m.get("bot_id") else "user"
            history.append({"role": role, "content": txt})
        return history
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live thread history fetch failed: %s", exc)
    return []


def _build_thread_context(client, channel: str, thread_ts: str, current_ts: str, full: bool = False) -> str:
    """Fetch Slack thread and return formatted context string.

    full=True  → include ALL non-bot messages (for summarization).
    full=False → return only the most recent non-bot substantive message.
    """
    try:
        replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        messages = replies.get("messages", [])
        # Exclude the current mention message itself
        messages = [m for m in messages if m.get("ts") != current_ts]

        if full:
            parts = []
            for m in messages:
                sender = m.get("username") or m.get("user") or m.get("bot_id", "bot")
                txt = _extract_message_text(m)
                if txt:
                    parts.append(f"[{sender}]: {txt}")
            return "\n".join(parts)
        else:
            # Always include the parent (root) message — alert bots post UDID/Host IP
            # in attachments/blocks there; it's the primary context for thread replies.
            parent_text = _extract_message_text(messages[0]) if messages else ""

            # Also grab the most recent substantive non-bot follow-up (if any)
            recent_user = ""
            for m in reversed(messages[1:]):
                if m.get("bot_id"):
                    continue
                prior = _extract_message_text(m)
                if not _is_thin_text(prior):
                    recent_user = prior
                    break

            if parent_text and recent_user:
                return f"{parent_text}\n{recent_user}"
            return parent_text or recent_user
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thread context fetch failed: %s", exc)
    return ""


# Simple greetings handled locally — no Gemini call needed

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


def _exec_create_jira(params: dict, slack_client=None) -> dict:
    """Create a Jira ticket using the full jira_client (ADF, custom fields, user resolution)."""
    title = params.get("title", "").strip()
    if not title:
        return {"success": False, "error": "Could not determine ticket title"}

    # Build a rich description from available params
    description_parts = []
    if params.get("description"):
        description_parts.append(params["description"])
    if params.get("host"):
        description_parts.append(f"Host: {params['host']}")
    if params.get("devices"):
        description_parts.append(f"Devices: {', '.join(params['devices'])}")
    if params.get("slack_thread_url"):
        description_parts.append(f"Slack thread: {params['slack_thread_url']}")
    description = "\n".join(description_parts) if description_parts else title

    # Resolve Slack user ID → JIRA accountId if provided
    assignee_jira_id: str | None = None
    raw_assignee = params.get("assignee", "")
    if raw_assignee:
        if raw_assignee.startswith("U") and slack_client:
            # Looks like a Slack user ID — resolve it
            assignee_jira_id = resolve_slack_user_to_jira(raw_assignee, slack_client)
        else:
            # Already a JIRA accountId
            assignee_jira_id = raw_assignee
    # Fall back to bot default assignee if not resolved
    if not assignee_jira_id:
        assignee_jira_id = settings.JIRA_ASSIGNEE_ID or None

    result = create_issue(
        title=title,
        description=description,
        assignee_jira_id=assignee_jira_id,
        priority=params.get("priority", "Medium"),
        labels=params.get("labels") or [],
        custom_overrides=params.get("custom_fields") or {},
    )
    # Carry through extra display fields
    result["cc"] = params.get("cc", []) or []
    result["issue_type"] = params.get("issue_type", "Task")
    # Pass the original Slack user ID separately so _jira_created_reply can format it safely
    raw_assignee = params.get("assignee", "")
    if raw_assignee and raw_assignee.startswith("U") and 8 <= len(raw_assignee) <= 12:
        result["slack_assignee_id"] = raw_assignee
    return result


def _exec_assign_ticket(params: dict, slack_client=None) -> dict:
    """Assign a Jira ticket, resolving Slack user IDs when needed."""
    ticket_key = params.get("ticket_key", "")
    raw_assignee = params.get("assignee", "")
    cc: list[str] = params.get("cc", []) or []

    if not ticket_key or not raw_assignee:
        return {"success": False, "error": "Need both ticket key and assignee"}

    # Resolve Slack → JIRA if it looks like a Slack user ID
    assignee_jira_id = raw_assignee
    if raw_assignee.startswith("U") and slack_client:
        resolved = resolve_slack_user_to_jira(raw_assignee, slack_client)
        if resolved:
            assignee_jira_id = resolved

    result = assign_issue(ticket_key, assignee_jira_id)
    result["cc"] = cc
    return result


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

    # For ssh_reboot: separate host IPs from device UDIDs/serials.
    # _run_bulk iterates over `devices` and SSH-connects to each — we must only include real IPs.
    # UDID goes into action_params["udid"] so SSHAction can run the right reboot command.
    _ip_re = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
    if action_type == "ssh_reboot":
        host_ips = [d for d in devices if _ip_re.match(d)]
        udid_list = [d for d in devices if not _ip_re.match(d)]
        ssh_host = params.get("host") or (host_ips[0] if host_ips else (devices[0] if devices else ""))
        ssh_udid = params.get("udid") or (udid_list[0] if udid_list else "")
        ssh_devices = host_ips if host_ips else ([ssh_host] if ssh_host else [])
    else:
        ssh_host = params.get("host") or (devices[0] if devices else "")
        ssh_udid = params.get("udid") or ""
        ssh_devices = devices

    action_params = {
        "devices": ssh_devices,
        "udid": ssh_udid,
        "host": ssh_host,
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


def _format_jira_created_blocks(result: dict) -> list[dict]:
    """Block Kit blocks for a successfully created Jira ticket."""
    key = result.get("ticket_key", "?")
    url = result.get("url", f"{JIRA_BROWSE}/{key}")
    title = result.get("title", key)
    assignee = result.get("assignee", "")
    cc: list[str] = result.get("cc", []) or []

    assignee_line = f"<@{assignee}>" if assignee and assignee.startswith("U") else (assignee or "_unassigned_")
    cc_line = " ".join(f"<@{u}>" for u in cc if u) if cc else ""

    meta_parts = [f"Project TE  \u00b7  Platform Engineering"]
    if cc_line:
        meta_parts.append(f"CC: {cc_line}")

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":white_check_mark: *Ticket Created* \u2014 <{url}|{key}>\n{title}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Assignee*\n{assignee_line}"},
                {"type": "mrkdwn", "text": f"*Priority*\n{result.get('priority', 'Medium')}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  \u00b7  ".join(meta_parts)}],
        },
    ]
    return blocks


def register_message_listeners(app: App) -> None:
    @app.event("app_mention")
    def handle_mention(event: dict, say, client) -> None:  # noqa: ANN001
        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user_id = event.get("user", "")

        logger.info("@mention from %s in %s", user_id, channel)

        # --- Authorization check — only allowlisted users may trigger actions ---
        if user_id not in AUTHORIZED_USER_IDS:
            say(text=_UNAUTHORIZED_REPLY, thread_ts=thread_ts)
            logger.warning("Unauthorized action attempt by %s: %.100s", user_id, text)
            return

        thread_history = thread_memory.format_for_claude(channel, thread_ts)
        thread_memory.add_message(channel, thread_ts, "user", text)

        # --- Enrich with thread context when needed ---
        # Case 1 (thin mention): just "👆" or emoji — prepend most recent substantive msg.
        # Case 2 (context-dependent): "Summaries the thread", "tldr" etc — prepend full history.
        # Case 3 (thread reply): any mention inside a thread.
        #   Replace Redis thread_history with LIVE Slack thread so Claude sees ALL messages —
        #   including non-@mention corrections (e.g. "use idevice_id not docker") that Redis
        #   never captures because it only stores @mention interactions.
        classify_text = text
        is_summarize = bool(_CONTEXT_DEPENDENT_RE.search(clean))
        is_thread_reply = thread_ts != event.get("ts", "")  # mentioned inside a thread

        if is_thread_reply:
            live_history = _build_live_thread_history(
                client, channel, thread_ts, event.get("ts", ""), limit=10
            )
            if live_history:
                thread_history = live_history
                logger.info("Thread reply: using live Slack history (%d msgs)", len(live_history))

        if _is_thin_text(clean) or is_summarize or is_thread_reply:
            ctx = _build_thread_context(
                client, channel, thread_ts, event.get("ts", ""),
                full=is_summarize,
            )
            if ctx:
                if is_summarize:
                    classify_text = (
                        f"Thread content to summarize:\n{ctx}\n\n"
                        f"User request: {text}"
                    )
                    logger.info("Summarize request — enriched with full thread (%d chars)", len(ctx))
                else:
                    classify_text = ctx + " " + text
                    logger.info("Thread reply enriched with context: %.80s", ctx)

        # Strip Slack formatting before classification so regex/Claude see clean text
        classify_text = _clean_slack_text(classify_text)

        # ── Claude CLI → classify/direct → Gemini fallback ───────────────────
        # Claude handles all intent detection including create_jira — the router
        # prompt has explicit priority rules so "create ticket" is never misclassified
        # as device_check even when thread context contains device data.
        classification = brain.classify(classify_text, thread_history=thread_history or None)
        source = classification.get("_source", "claude")

        intent = classification.get("intent", "unknown")
        params = classification.get("params", {})
        confidence = classification.get("confidence", 0.0)

        logger.info("Classified [%s]: intent=%s confidence=%.2f", source, intent, confidence)
        log_user_request(user_id, channel, classify_text[:150], intent, confidence, source)

        # --- Quota exceeded — surface friendly message instead of crashing ---
        if intent == "_quota_exceeded":
            from bot.nlp.claude_brain import _QUOTA_MSG
            say(text=_QUOTA_MSG, thread_ts=thread_ts)
            return

        # --- Claude direct reply — Claude handled it intelligently, post as-is ---
        if intent == "_direct_reply":
            reply = params.get("reply", "")
            if reply:
                say(text=reply, thread_ts=thread_ts)
                thread_memory.add_message(channel, thread_ts, "assistant", reply)
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

        if intent == "device_check":
            from bot.actions.device_check_action import DeviceCheckAction  # noqa: PLC0415
            host = params.get("host", "") or ""
            udid = params.get("udid", "") or ""
            hosts = params.get("hosts") or []
            udids = params.get("udids") or []
            log_lines = int(params.get("log_lines") or 20)
            # Flatten: Claude sometimes returns dicts inside devices/hosts/udids lists
            def _flat_str(v) -> str:  # noqa: ANN001
                if isinstance(v, str):
                    return v
                if isinstance(v, dict):
                    return v.get("host") or v.get("ip") or v.get("udid") or v.get("serial") or ""
                return str(v) if v else ""
            hosts = [_flat_str(h) for h in hosts if h]
            udids = [_flat_str(u) for u in udids if u]
            # If host not set but devices list has IPs vs serials, split them
            if not host:
                devices_list = [_flat_str(d) for d in params.get("devices", []) if d]
                host = next((d for d in devices_list if isinstance(d, str) and d.startswith("10.")), "")
                udid = udid or next((d for d in devices_list if isinstance(d, str) and not d.startswith("10.")), "")
            bot_reply = DeviceCheckAction().execute(host, udid, hosts=hosts, udids=udids, log_lines=log_lines)
            say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "create_jira":
            result = _exec_create_jira(params, slack_client=client)
            if result.get("success"):
                blocks = _format_jira_created_blocks(result)
                say(blocks=blocks, text=_jira_created_reply(result), thread_ts=thread_ts)
                bot_reply = _jira_created_reply(result)
            else:
                bot_reply = _jira_created_reply(result)
                say(text=bot_reply, thread_ts=thread_ts)

        elif intent == "assign_ticket":
            result = _exec_assign_ticket(params, slack_client=client)
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
        if user_id not in AUTHORIZED_USER_IDS:
            client.chat_postMessage(channel=channel, text=_UNAUTHORIZED_REPLY)
            return
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

        if intent == "device_check":
            from bot.actions.device_check_action import DeviceCheckAction  # noqa: PLC0415
            host = params.get("host", "")
            udid = params.get("udid", "")
            reply = DeviceCheckAction().execute(host, udid)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
        elif intent == "create_jira":
            result = _exec_create_jira(params, slack_client=client)
            if result.get("success"):
                blocks = _format_jira_created_blocks(result)
                client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                        blocks=blocks, text=_jira_created_reply(result))
            else:
                client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                        text=_jira_created_reply(result))
        elif intent == "assign_ticket":
            result = _exec_assign_ticket(params, slack_client=client)
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
