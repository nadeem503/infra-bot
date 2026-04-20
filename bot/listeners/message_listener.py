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
from bot.memory.active_threads import activate as activate_thread, is_active as thread_is_active
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
    get_account_display_name,
)
from config import settings
from utils.device_name import get_device_name
from utils.logger import get_logger

logger = get_logger(__name__)
_formatter = SlackFormatter()

JIRA_BROWSE = "https://lambdatest.atlassian.net/browse"

CONFIDENCE_THRESHOLD = 0.6

# Action types that skip the bot approval card and execute immediately.
# The GitHub Actions workflow itself has environment protection gates for prod approval.
# After triggering, the bot notifies MOBILE_INFRA_SLACK_ID for awareness.
_AUTO_EXECUTE_ACTIONS: frozenset[str] = frozenset({"device_dispose", "device_migrate", "db_query", "faulty_devices_report"})

# Only these users may trigger infra actions (device checks, restarts, Jira, etc.)
# All other users receive greetings/capability replies only.
AUTHORIZED_USER_IDS: frozenset[str] = frozenset({
    "U04UTG30V9A",  # Nadeem Khan
    "U03GPJ43TJT",  # Pratik Parmar
    "U020L115A2X",  # Shivnarayan Shishodia
    "U093GFRUUUT",  # Somasekhar Avula
    "U07T3P36R2M",  # Sunny Kumar
    "U07TQLSPSQ2",  # Omveer Panwar
})


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

# Fix #6: maximum characters sent to the classifier.
# Thread context + user text can balloon to several KB on busy threads — this caps
# the Redis cache key size and keeps Claude prompts lean. The tail is kept because
# recent context (the user's latest message) matters more than the thread opener.
_CLASSIFY_TEXT_MAX = 1200


# Fix #5: moved from inside handle_mention (was re-created on every mention).
# Coerces a device/host value to a plain string — Claude sometimes returns dicts
# e.g. {"host": "10.x.x.x"} instead of a bare string inside devices/hosts lists.
def _flat_str(v) -> str:  # noqa: ANN001
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("host") or v.get("ip") or v.get("udid") or v.get("serial") or ""
    return str(v) if v else ""


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


def _build_live_thread_history(
    client, channel: str, thread_ts: str, current_ts: str,
    limit: int = 10, prefetched: list | None = None,
) -> list[dict]:
    """Build Claude thread_history from a live Slack thread.

    Captures ALL messages (including non-@mention user messages) so corrections
    like "use idevice_id not docker" posted without @mention are visible to Claude.
    Pass prefetched=<messages list> to reuse an already-fetched conversations_replies
    response — avoids a duplicate API call when _build_thread_context is also needed.
    Returns list of {"role": "user"|"assistant", "content": str} dicts.
    """
    try:
        # Fix #4: accept pre-fetched messages to avoid a second conversations_replies call
        if prefetched is not None:
            messages = prefetched
        else:
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


def _build_thread_context(
    client, channel: str, thread_ts: str, current_ts: str,
    full: bool = False, prefetched: list | None = None,
) -> str:
    """Build a formatted thread context string from a Slack thread.

    full=True  → include ALL non-bot messages (for summarization).
    full=False → return parent message + most recent substantive user message.
    Pass prefetched=<messages list> to reuse an already-fetched conversations_replies
    response — avoids a duplicate API call when _build_live_thread_history is also needed.
    """
    try:
        # Fix #4: accept pre-fetched messages to avoid a second conversations_replies call
        if prefetched is not None:
            messages = prefetched
        else:
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
    "db_query":                 "db_query",
    "faulty_devices_report":    "faulty_devices_report",
    "jenkins_failure":          "jenkins_trigger",
    "jenkins_trigger":          "jenkins_trigger",
    "jenkins_search":           "jenkins_search",
    "jenkins_params":           "jenkins_search",
    "jenkins_status":           "jenkins_search",
    "app_crash":                "adb_logcat",
    "storage_issue":            "adb_clear_storage",
    "device_disconnected":      "device_disconnected",
    # Device lifecycle (GitHub Actions workflows on LambdatestIncPrivate/migrations)
    "device_dispose":           "device_dispose",
    "device_migrate":           "device_migrate",
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
    from bot.actions.db_action import DBAction, FaultyDevicesReportAction  # noqa: PLC0415
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
    from bot.actions.device_lifecycle_action import (  # noqa: PLC0415
        DeviceDisposeAction, DeviceHostUpdateAction,
    )
    return {
        # Generic device actions
        "ssh_reboot":                SSHAction,
        "device_status":             DeviceStatusAction,
        "adb_restart":               ADBAction,
        "adb_logcat":                ADBAction,
        "adb_clear_storage":         ADBAction,
        "db_query":                  DBAction,
        "faulty_devices_report":     FaultyDevicesReportAction,
        "jenkins_trigger":           JenkinsAction,
        "device_disconnected":       DeviceDisconnectedAction,
        # Device lifecycle — GitHub Actions workflow_dispatch
        "device_dispose":            DeviceDisposeAction,
        "device_migrate":            DeviceHostUpdateAction,
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

    # Build a rich structured description from all available params
    sections: list[str] = []

    # Problem / summary section
    problem_text = params.get("description") or title
    sections.append(f"*Problem*\n{problem_text}")

    # Affected devices section
    device_lines: list[str] = []
    if params.get("host"):
        device_lines.append(f"Host IP: {params['host']}")
    if params.get("udid"):
        device_lines.append(f"UDID: {params['udid']}")
    if params.get("devices"):
        devs = params["devices"]
        if isinstance(devs, list) and devs:
            device_lines.append(f"Devices: {', '.join(str(d) for d in devs)}")
    if params.get("host_udid_pairs"):
        device_lines.append(f"Host/UDID pairs: {params['host_udid_pairs']}")
    if params.get("udids"):
        device_lines.append(f"UDIDs: {params['udids']}")
    if params.get("host_ips"):
        device_lines.append(f"Host IPs: {params['host_ips']}")
    if device_lines:
        sections.append("*Affected Devices*\n" + "\n".join(f"• {line}" for line in device_lines))

    # Action / context section
    context_lines: list[str] = []
    if params.get("environment"):
        context_lines.append(f"Environment: {params['environment']}")
    if params.get("remark"):
        context_lines.append(f"Remark: {params['remark']}")
    if params.get("status"):
        context_lines.append(f"Status change: → {params['status']}")
    if params.get("dedicated_org"):
        context_lines.append(f"Org: {params['dedicated_org']}")
    if context_lines:
        sections.append("*Details*\n" + "\n".join(f"• {line}" for line in context_lines))

    # Reference section
    ref_lines: list[str] = []
    ref_lines.append("Raised via Infra-Bot (Slack)")
    if params.get("slack_thread_url"):
        ref_lines.append(f"Slack thread: {params['slack_thread_url']}")
    sections.append("*Reference*\n" + "\n".join(f"• {line}" for line in ref_lines))

    description = "\n\n".join(sections)

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
    # Resolve Jira account ID → human display name for the blocks card
    jira_assignee_id = result.get("assignee", "")
    if jira_assignee_id:
        result["assignee_name"] = get_account_display_name(jira_assignee_id)
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


def _handle_thread_monitor(params: dict, channel: str, thread_ts: str, user_id: str, say) -> str:
    """Start a Redis-backed thread monitor. ping_message comes fully from Claude params."""
    from bot.memory.monitor_store import create_monitor  # noqa: PLC0415

    target_name    = params.get("target_name", "").strip()
    target_user_id = params.get("target_user_id", "").strip()
    interval_min   = int(params.get("interval_minutes") or 5)
    ping_message   = params.get("ping_message", "").strip()

    if not ping_message:
        reply = ":warning: Couldn't compose a reminder — please describe who to ping and what about."
        say(text=reply, thread_ts=thread_ts)
        return reply

    interval_sec = max(60, interval_min * 60)

    job_id = create_monitor(
        channel=channel,
        thread_ts=thread_ts,
        started_by=user_id,
        target_user_id=target_user_id,
        target_name=target_name,
        interval_seconds=interval_sec,
        ping_message=ping_message,
    )

    display = target_name or target_user_id or "the target"
    reply = (
        f":white_check_mark: Got it — I'll ping *{display}* every *{interval_min} min* in this thread.\n"
        f"Say `stop monitoring` or `confirm` when you're done. _(Job: `{job_id}`)_"
    )
    say(text=reply, thread_ts=thread_ts)
    return reply


def _handle_stop_monitor(channel: str, thread_ts: str, user_id: str, say) -> str:
    """Cancel all active monitors started by this user in this thread."""
    from bot.memory.monitor_store import cancel_monitors_by_user  # noqa: PLC0415

    count = cancel_monitors_by_user(user_id, channel, thread_ts)
    reply = (
        f":white_check_mark: Stopped {count} monitor job(s) in this thread."
        if count else
        ":grey_question: No active monitors found in this thread."
    )
    say(text=reply, thread_ts=thread_ts)
    return reply


def _handle_multi_action(
    actions: list[dict],
    text: str,
    channel: str,
    thread_ts: str,
    user_id: str,
    say,
    client,
    trace_id: str = "",
) -> str:
    """Execute multiple actions in sequence, piping outputs (e.g. jira_key) between steps.

    Sentinel values in params:
      "__from_jira__" → replaced with the Jira key produced by a preceding create_jira step.

    For infra_issue steps: _handle_infra_issue() calls say() internally (auto-execute),
    so we don't double-post.  For create_jira steps: we post the result here.
    """
    context: dict = {}   # carries outputs between steps, e.g. {"jira_key": "TE-12345"}

    for i, step in enumerate(actions):
        intent  = step.get("intent", "")
        params  = dict(step.get("params") or {})
        issue_category = step.get("issue_category") or params.get("issue_category", "")

        # Resolve sentinels — substitute outputs from previous steps
        for k, v in params.items():
            if v == "__from_jira__":
                params[k] = context.get("jira_key", "")

        logger.info("[%s] Multi-action step %d/%d: intent=%s", trace_id, i + 1, len(actions), intent)

        if intent == "create_jira":
            result = _exec_create_jira(params, slack_client=client)
            if result.get("success"):
                jira_key = result.get("key", "")
                context["jira_key"] = jira_key
                # Post the Jira creation result
                try:
                    blocks = _format_jira_created_blocks(result)
                    say(blocks=blocks, text=_jira_created_reply(result), thread_ts=thread_ts)
                except Exception:  # noqa: BLE001
                    say(text=_jira_created_reply(result), thread_ts=thread_ts)
            else:
                say(text=_jira_created_reply(result), thread_ts=thread_ts)
                logger.error("[%s] Multi-action: create_jira failed at step %d — stopping chain", trace_id, i + 1)
                return f"[Multi-action] stopped at step {i + 1}: create_jira failed"

        elif intent == "infra_issue":
            if issue_category:
                params["issue_category"] = issue_category
            _handle_infra_issue(params, text, channel, thread_ts, user_id, say, client, trace_id=trace_id)

        else:
            logger.warning("[%s] Multi-action: unsupported intent %s at step %d", trace_id, intent, i + 1)

    return f"[Multi-action] {len(actions)} steps completed"


def _handle_jenkins_search(params: dict, thread_ts: str, say) -> str:  # noqa: ANN001
    """List Jenkins jobs matching the user's description — no trigger."""
    from utils.jenkins_client import search_jobs, get_job_params, list_jobs  # noqa: PLC0415
    query = (params.get("job_query") or params.get("job_name") or "").strip()
    if not query:
        # No query — list all jobs
        jobs = list_jobs()
        if not jobs:
            say(text=":jenkins: No Jenkins jobs found (check JENKINS_URL config)", thread_ts=thread_ts)
            return "jenkins_search: no jobs"
        preview = "\n".join(f"  • `{j}`" for j in jobs[:20])
        suffix = f"\n_…and {len(jobs) - 20} more_" if len(jobs) > 20 else ""
        say(text=f":jenkins: *All Jenkins jobs ({len(jobs)} total):*\n{preview}{suffix}", thread_ts=thread_ts)
        return f"jenkins_search: listed {len(jobs)} jobs"

    matches = search_jobs(query)
    if not matches:
        say(text=f":jenkins: No Jenkins jobs found matching `{query}`", thread_ts=thread_ts)
        return "jenkins_search: no matches"

    lines = [f":jenkins: *Found {len(matches)} job(s) matching `{query}`:*"]
    for j in matches:
        lines.append(f"  • `{j}`")
    lines.append("_Reply with a job name + params to trigger one._")
    say(text="\n".join(lines), thread_ts=thread_ts)
    return f"jenkins_search: {len(matches)} matches for '{query}'"


def _handle_jenkins_params(params: dict, thread_ts: str, say) -> str:  # noqa: ANN001
    """Show parameters for a Jenkins job — no trigger."""
    from utils.jenkins_client import search_job, get_job_params  # noqa: PLC0415
    raw_name = (params.get("job_name") or params.get("job_query") or "").strip()
    if not raw_name:
        say(text=":warning: Please specify a job name, e.g. `show params for ubuntu host setup`", thread_ts=thread_ts)
        return "jenkins_params: no job name"

    job_name = search_job(raw_name)
    if not job_name:
        say(text=f":jenkins: No job found matching `{raw_name}`", thread_ts=thread_ts)
        return "jenkins_params: no match"

    param_defs = get_job_params(job_name)
    if not param_defs:
        say(text=f":jenkins: Job `{job_name}` has no defined parameters (or params unavailable)", thread_ts=thread_ts)
        return f"jenkins_params: {job_name} no params"

    lines = [f":jenkins: *Parameters for `{job_name}`:*"]
    for p in param_defs:
        desc = f" — _{p['description']}_" if p.get("description") else ""
        default = f" (default: `{p['default']}`)" if p.get("default") not in (None, "") else ""
        lines.append(f"  • `{p['name']}`{default}{desc}")
    say(text="\n".join(lines), thread_ts=thread_ts)
    return f"jenkins_params: listed params for {job_name}"


def _handle_jenkins_status(params: dict, channel: str, thread_ts: str, say) -> str:  # noqa: ANN001
    """Check the status of a recent Jenkins build — polls Jenkins API."""
    from utils.jenkins_monitor import (  # noqa: PLC0415
        get_recent_build_for_thread, get_build_status, get_pending_builds,
    )
    from utils.jenkins_client import search_job  # noqa: PLC0415

    # Try to find the build from params first, then fall back to thread context
    job_name  = (params.get("job_name") or "").strip()
    build_num = int(params.get("build_num") or 0)

    build_meta = None

    if job_name and build_num:
        # User specified exact job + build number
        resolved = search_job(job_name) if job_name else job_name
        if resolved:
            build_meta = {"job_name": resolved, "build_num": build_num,
                          "build_url": "", "triggered_by": ""}

    if not build_meta:
        # Look up most recent build triggered in this thread
        build_meta = get_recent_build_for_thread(channel, thread_ts)

    if not build_meta:
        # Last resort: most recent pending build overall
        pending = get_pending_builds()
        if pending:
            build_meta = max(pending, key=lambda b: b.get("triggered_at", 0))

    if not build_meta:
        say(text=":jenkins: No recent build found. Trigger a job first, or specify `job name + build number`.", thread_ts=thread_ts)
        return "jenkins_status: no build found"

    jname = build_meta["job_name"]
    bnum  = build_meta["build_num"]
    burl  = build_meta.get("build_url", "")

    status = get_build_status(jname, bnum)
    if status is None:
        say(text=f":warning: Could not reach Jenkins to check `{jname}` #{bnum}. Try opening the build link directly.", thread_ts=thread_ts)
        return "jenkins_status: unreachable"

    if status.get("building"):
        say(text=f":hourglass_flowing_sand: `{jname}` #{bnum} is *still running*…\n:link: <{burl}|View build>", thread_ts=thread_ts)
        return f"jenkins_status: {jname}#{bnum} building"

    result = status.get("result", "UNKNOWN")
    icons  = {"SUCCESS": ":white_check_mark:", "FAILURE": ":x:", "ABORTED": ":no_entry:", "UNSTABLE": ":warning:"}
    icon   = icons.get(result, ":grey_question:")
    dur    = status.get("duration_s", 0)
    dur_str = f"{dur // 60}m {dur % 60}s" if dur >= 60 else f"{dur}s"
    link   = f"<{burl}|#{bnum}>" if burl else f"#{bnum}"
    say(
        text=f"{icon} `{jname}` {link} — *{result}* ({dur_str})",
        thread_ts=thread_ts,
    )
    return f"jenkins_status: {jname}#{bnum} → {result}"


def _handle_infra_issue(
    params: dict,
    text: str,
    channel: str,
    thread_ts: str,
    user_id: str,
    say,
    client=None,
    trace_id: str = "",
) -> str:
    issue_category = params.get("issue_category", "device_down")

    # --- Informational Jenkins queries — no approval flow needed ---
    if issue_category == "jenkins_search":
        return _handle_jenkins_search(params, thread_ts, say)
    if issue_category == "jenkins_params":
        return _handle_jenkins_params(params, thread_ts, say)
    if issue_category == "jenkins_status":
        return _handle_jenkins_status(params, channel, thread_ts, say)
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
        "thread_ts": thread_ts,
        "summary": f"[Infra-Bot] {issue_category} in {region_display}",
        "description": f"Detected via Slack: {text[:500]}",
    }
    # Merge extra params from Claude — GitHub workflow actions need fields like
    # jira, environment, dedicated_org, host_udid_pairs, udids, host_ips, cleanup,
    # remark, status that don't exist in the standard action_params above.
    # Existing keys are NOT overwritten — standard params take precedence.
    for _k, _v in params.items():
        if _k not in action_params and _v is not None and _v != "":
            action_params[_k] = _v

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

    # --- Auto-execute path for lifecycle workflow actions ---
    # device_dispose and device_migrate skip the bot approval card entirely.
    # The GitHub Actions workflow already has environment protection gates.
    # We trigger the WF immediately, post the result, then notify mobile-infra.
    if action_type in _AUTO_EXECUTE_ACTIONS:
        ActionClass = _get_action_class(action_type)
        if ActionClass:
            set_last_action(user_id, action_type)
            action = ActionClass(
                params=action_params, triggered_by=user_id,
                channel=channel, region=region_slug,
            )
            try:
                result = action.execute()
            except Exception as exc:  # noqa: BLE001
                logger.error("Auto-execute %s failed: %s", action_type, exc)
                result = {"success": False, "message": f"Execution error: {type(exc).__name__}", "details": {}}

            # db_query / faulty_devices_report: format rows as a Slack table
            if action_type in ("db_query", "faulty_devices_report"):
                result_text = _formatter.format_db_result(result)
                say(text=result_text, thread_ts=thread_ts)
                logger.info("Auto-executed %s for %s region=%s", action_type, issue_category, region_slug)
                return f"[Auto-executed] `{action_type}` for `{issue_category}` in {region_slug}"

            # For lifecycle workflow actions skip the generic "Action Completed: device_migrate"
            # header from format_result — the action's own message already has all the detail
            # and the workflow approval link.
            if result.get("success"):
                result_text = result.get("message", f":rocket: `{action_type}` triggered")
            else:
                result_text = _formatter.format_error(result.get("message", f"`{action_type}` failed"))

            # Tag mobile-infra team for GH Actions workflow approval
            if result.get("success") and settings.MOBILE_INFRA_SLACK_ID:
                notify_id = settings.MOBILE_INFRA_SLACK_ID
                if notify_id.startswith("S"):
                    mention = f"<!subteam^{notify_id}>"
                elif notify_id.startswith("C"):
                    mention = f"<#{notify_id}>"
                else:
                    mention = f"<@{notify_id}>"
                result_text += f"\n\n{mention} please review and approve the workflow run above."

            say(text=result_text, thread_ts=thread_ts)

            # Store workflow run for 1h approval reminder
            if result.get("success") and action_type in ("device_dispose", "device_migrate"):
                try:
                    from bot.workers.wf_approval_poller import store_pending_run  # noqa: PLC0415
                    runs_url = result.get("runs_url", "") or ""
                    store_pending_run(
                        runs_url=runs_url,
                        channel=channel,
                        thread_ts=thread_ts,
                        triggered_by=user_id,
                        action_type=action_type,
                    )
                except Exception:  # noqa: BLE001
                    pass
            logger.info("Auto-executed %s for %s region=%s", action_type, issue_category, region_slug)
            return f"[Auto-executed] `{action_type}` for `{issue_category}` in {region_slug}"
        else:
            say(text=_formatter.format_error(f"No handler for `{action_type}`"), thread_ts=thread_ts)
            return f"No handler for {action_type}"

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
    from bot.memory import learning_store, pattern_store  # noqa: PLC0415
    recommendation = learning_store.get_recommendation(issue_category, region_slug)
    # Fetch any operator-noted patterns for this issue_type so approvers see
    # "an operator previously fixed this with: reboot + reload LRR plist"
    prior_patterns = pattern_store.get_patterns(issue_category, region_slug, limit=2)

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
        trace_id=trace_id,
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
        prior_patterns=prior_patterns,
    )

    resp = say(blocks=blocks, text=f"Infra-Bot: {issue_category}", thread_ts=thread_ts)

    # Store message ts for audit trail editing + reaction approval
    if resp and resp.get("ts") and client:
        approval_manager.set_msg_ts(action_id, resp["ts"], channel)

    # Start escalation watcher
    if client:
        approval_manager.start_escalation_watcher(action_id, channel, thread_ts, client)

    logger.info("[%s] Infra issue posted: action_id=%s %s region=%s devices=%s",
                trace_id, action_id, issue_category, region_slug, devices)
    # Fix #2: return a descriptive string that gets stored in thread_memory as the bot reply.
    # Previously stored "Analyzing device_down — approval required" — Claude couldn't answer
    # follow-up questions like "what action did you propose?" or "what's the action ID?"
    # because the actual details (action_id, action_type, devices) weren't in memory.
    device_summary = ", ".join(devices[:3]) + (f" +{len(devices) - 3} more" if len(devices) > 3 else "")
    return (
        f"[Action {action_id}] Proposed `{action_type}` for `{issue_category}` in {region_slug}"
        + (f" | devices: {device_summary}" if device_summary else "")
        + " — awaiting approval"
    )


def _format_jira_created_blocks(result: dict) -> list[dict]:
    """Block Kit blocks for a successfully created Jira ticket."""
    key = result.get("ticket_key", "?")
    url = result.get("url", f"{JIRA_BROWSE}/{key}")
    title = result.get("title", key)
    cc: list[str] = result.get("cc", []) or []

    # Use resolved display name if available, fall back to Slack mention or raw ID
    slack_id = result.get("slack_assignee_id", "")
    assignee_name = result.get("assignee_name", "")
    if slack_id:
        assignee_line = f"<@{slack_id}>"
    elif assignee_name:
        assignee_line = assignee_name
    else:
        assignee_line = "_unassigned_"
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


def _handle_note_pattern(
    params: dict,
    channel: str,
    thread_ts: str,
    user_id: str,
    say,
) -> str:
    """Handle 'note_pattern' intent: store fix pattern and acknowledge in thread.

    Use case: operator says "Note the pattern for future fix @Infra-bot. This device
    is fixed nothing to do." Claude extracts UDID, host, issue_type, pattern description,
    and fix steps from the message + thread context. We store them in pattern_store and
    reply with a formatted acknowledgment mirroring what the operator saw.

    The stored patterns are surfaced on future approval cards for the same issue_type
    so the next approver can see "an operator noted: reboot + reload LRR plist fixes this".
    """
    from bot.memory import pattern_store  # noqa: PLC0415

    udid         = params.get("udid", "")
    host         = params.get("host", "")
    issue_type   = params.get("issue_type") or params.get("issue_category") or "general"
    pattern_text = params.get("pattern", "")
    steps: list[str] = params.get("steps") or []
    fixed        = bool(params.get("fixed", False))
    device_name  = params.get("device_name", "")
    region       = params.get("region") or "unknown"

    # Persist the pattern if there is anything to store
    if pattern_text or steps:
        pattern_store.save_pattern(
            udid=udid,
            host=host,
            issue_type=issue_type,
            pattern=pattern_text,
            steps=steps,
            region=region,
            saved_by=user_id,
            device_name=device_name,
        )
        logger.info("Pattern saved: issue_type=%s region=%s udid=%.12s by=%s",
                    issue_type, region, udid, user_id)

    # ── Build reply ──────────────────────────────────────────────────────────
    lines: list[str] = []

    # Header: device confirmed fixed (or just pattern noted with no device context)
    if fixed and (udid or host):
        dev_label = f"`{udid}`"
        if device_name:
            dev_label += f" ({device_name})"
        host_label = f" on `{host}`" if host else ""
        lines.append(
            f":white_check_mark: Got it! Device {dev_label}{host_label} "
            f"is confirmed *fixed* \u2014 no action needed."
        )
    elif pattern_text or steps:
        lines.append(":brain: Got it! Pattern noted.")
    else:
        lines.append(":white_check_mark: Noted! I didn't catch specific steps — feel free to add them.")

    # Pattern body
    if pattern_text or steps:
        lines.append("")
        lines.append("*Pattern noted for future reference:*")
        if pattern_text:
            lines.append(f"\u2022 {pattern_text}")
        if steps:
            # Render steps as inline arrow chain: `step1` → `step2` → `step3`
            steps_chain = " \u2192 ".join(f"`{s}`" for s in steps)
            lines.append(f"\u2022 *Steps:* {steps_chain}")

    reply = "\n".join(lines)
    say(text=reply, thread_ts=thread_ts)
    return reply


def register_message_listeners(app: App) -> None:
    @app.event("app_mention")
    def handle_mention(event: dict, say, client) -> None:  # noqa: ANN001
        # Fix #11: trace_id links every log line for this request — grep it to follow
        # the full path: classify → approval create → action execute → result
        trace_id = uuid.uuid4().hex[:8]

        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user_id = event.get("user", "")

        logger.info("[%s] @mention from %s in %s: %.80s", trace_id, user_id, channel, text)

        # --- Authorization check — only allowlisted users may trigger actions ---
        if user_id not in AUTHORIZED_USER_IDS:
            clean_text = _clean_slack_text(text)
            greeting = brain.generate_unauthorized_greeting(clean_text)
            say(text=greeting, thread_ts=thread_ts)
            logger.warning("[%s] Unauthorized attempt by %s: %.100s", trace_id, user_id, text)
            return

        # Mark thread as active so follow-up messages (without @mention) are also processed
        activate_thread(channel, thread_ts, activated_by=user_id)

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
        clean = text.split(">", 1)[-1].strip().lower().rstrip("?! ")
        is_summarize = bool(_CONTEXT_DEPENDENT_RE.search(clean))
        is_thread_reply = thread_ts != event.get("ts", "")  # mentioned inside a thread
        current_ts = event.get("ts", "")

        # Fix #4: fetch the Slack thread once and share the result between
        # _build_live_thread_history and _build_thread_context.
        # Previously both functions independently called conversations_replies — two
        # identical API calls for every thread reply or thin-text mention.
        prefetched_thread: list[dict] | None = None
        if is_thread_reply or _is_thin_text(clean) or is_summarize:
            try:
                _r = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
                prefetched_thread = _r.get("messages", [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Thread prefetch failed: %s", trace_id, exc)
                prefetched_thread = []

        if is_thread_reply:
            live_history = _build_live_thread_history(
                client, channel, thread_ts, current_ts, limit=10,
                prefetched=prefetched_thread,
            )
            if live_history:
                thread_history = live_history
                logger.info("[%s] Thread reply: using live Slack history (%d msgs)", trace_id, len(live_history))

        if _is_thin_text(clean) or is_summarize or is_thread_reply:
            ctx = _build_thread_context(
                client, channel, thread_ts, current_ts,
                full=is_summarize, prefetched=prefetched_thread,
            )
            if ctx:
                if is_summarize:
                    classify_text = (
                        f"Thread content to summarize:\n{ctx}\n\n"
                        f"User request: {text}"
                    )
                    logger.info("[%s] Summarize request — enriched with full thread (%d chars)", trace_id, len(ctx))
                else:
                    classify_text = ctx + " " + text
                    logger.info("[%s] Thread reply enriched with context: %.80s", trace_id, ctx)

        # Strip Slack formatting before classification so regex/Claude see clean text
        classify_text = _clean_slack_text(classify_text)

        # Fix #6: cap classify_text so very long threads don't bloat the Redis cache key
        # and Claude prompt. Keep the tail — the user's latest message is the most relevant.
        if len(classify_text) > _CLASSIFY_TEXT_MAX:
            classify_text = classify_text[-_CLASSIFY_TEXT_MAX:]
            logger.debug("[%s] classify_text capped at %d chars", trace_id, _CLASSIFY_TEXT_MAX)

        # ── Claude CLI → classify/direct → Gemini fallback ───────────────────
        # Claude handles all intent detection including create_jira — the router
        # prompt has explicit priority rules so "create ticket" is never misclassified
        # as device_check even when thread context contains device data.
        classification = brain.classify(classify_text, thread_history=thread_history or None)
        source = classification.get("_source", "claude")

        intent = classification.get("intent", "unknown")
        params = classification.get("params", {})
        confidence = classification.get("confidence", 0.0)

        logger.info("[%s] Classified [%s]: intent=%s confidence=%.2f", trace_id, source, intent, confidence)
        log_user_request(user_id, channel, classify_text[:150], intent, confidence, source)

        # --- Quota exceeded — surface friendly message instead of crashing ---
        if intent == "_quota_exceeded":
            from bot.nlp.claude_brain import _QUOTA_MSG
            say(text=_QUOTA_MSG, thread_ts=thread_ts)
            return

        # Fix #3: confidence gate — post A/B/C clarification card when the classifier
        # is below threshold. Previously CONFIDENCE_THRESHOLD was defined but never checked,
        # so the bot acted on every low-confidence classification without asking the user.
        # Skip for: _direct_reply / _quota_exceeded (already handled above), and "unknown"
        # (falls through to _unclear_reply which already asks for clarification).
        _SKIP_GATE = frozenset({"_direct_reply", "_quota_exceeded", "unknown"})
        if confidence < CONFIDENCE_THRESHOLD and intent not in _SKIP_GATE:
            clarify_id = uuid.uuid4().hex[:8]
            # Offer: detected intent | generic device-check | "rephrase" escape hatch
            _options: list[dict] = [
                {"intent": intent, "params": params,
                 "label": intent.replace("_", " ").title()},
                {"intent": "device_check", "params": params,
                 "label": "Check device status only"},
                {"intent": "unknown", "params": {},
                 "label": "Neither \u2014 I\u2019ll rephrase"},
            ]
            # Deduplicate in case detected intent is already "device_check"
            _seen: set[str] = set()
            unique_opts: list[dict] = []
            for o in _options:
                if o["intent"] not in _seen:
                    _seen.add(o["intent"])
                    unique_opts.append(o)
            get_redis().setex(
                f"infra:clarify:{clarify_id}", 300,
                json.dumps({"options": unique_opts, "text": classify_text}),
            )
            blocks = _formatter.format_clarification_card(clarify_id, unique_opts)
            say(blocks=blocks, text="Not sure what you mean — please clarify.", thread_ts=thread_ts)
            logger.info(
                "[%s] Low confidence (%.2f) — clarification card posted (intent=%s)",
                trace_id, confidence, intent,
            )
            return

        # --- Multi-action: execute sequential steps, piping outputs between them ---
        if intent == "_multi_action":
            actions_list = params.get("actions", [])
            logger.info("[%s] Multi-action: %d steps", trace_id, len(actions_list))
            bot_reply = _handle_multi_action(
                actions_list, text, channel, thread_ts, user_id, say, client, trace_id=trace_id
            )
            thread_memory.add_message(channel, thread_ts, "assistant", f"[executed {len(actions_list)} actions]")
            return

        # --- Claude direct reply — Claude handled it intelligently, post as-is ---
        if intent == "_direct_reply":
            reply = params.get("reply", "")
            if reply:
                say(text=reply, thread_ts=thread_ts)
                thread_memory.add_message(channel, thread_ts, "assistant", reply)
            return

        # --- Confidence gating: low-confidence non-unknown intents fall through to
        # their intent handlers and, if still unresolved, hit the _unclear_reply path. ---

        bot_reply: str | None = None

        if intent == "device_check":
            from bot.actions.device_check_action import DeviceCheckAction, check_android_with_db  # noqa: PLC0415
            host = params.get("host", "") or ""
            udid = params.get("udid", "") or ""
            hosts = params.get("hosts") or []
            udids = params.get("udids") or []
            log_lines = int(params.get("log_lines") or 20)
            # Flatten: Claude sometimes returns dicts inside devices/hosts/udids lists
            # _flat_str is defined at module level (Fix #5)
            hosts = [_flat_str(h) for h in hosts if h]
            udids = [_flat_str(u) for u in udids if u]
            # Guard: Claude sometimes puts host IPs into udids (e.g. extracts IP from
            # hostname string "ubuntu-10-146-2-55") causing host_ip→host_ip pairing.
            udids = [u for u in udids if not _ip_re.match(u)]
            hosts = [h for h in hosts if _ip_re.match(h)]
            # If host not set but devices list has IPs vs serials, split them
            if not host:
                devices_list = [_flat_str(d) for d in params.get("devices", []) if d]
                host = next((d for d in devices_list if isinstance(d, str) and _ip_re.match(d)), "")
                udid = udid or next((d for d in devices_list if isinstance(d, str) and not _ip_re.match(d)), "")

            # Single-device: use combined go-adb + container adb + DB check (Android)
            if host and udid and not (hosts and len(hosts) > 1):
                from bot.actions.device_check_action import _resolve_host_type  # noqa: PLC0415
                host_type = _resolve_host_type(host, udid)
                if host_type == "ubuntu":
                    try:
                        from bot.actions.db_action import DBAction  # noqa: PLC0415
                        db_result = DBAction(
                            params={"query": (
                                "SELECT udid, host_ip, name, os_version, status, dedicated_org, "
                                f"region FROM device_host WHERE udid = '{udid}' LIMIT 1"
                            )},
                            triggered_by=user_id, channel=channel, region="",
                        ).execute()
                        db_row = (db_result.get("details", {}).get("rows") or [None])[0]
                    except Exception:  # noqa: BLE001
                        db_row = None
                    bot_reply = check_android_with_db(host, udid, db_row=db_row)
                else:
                    bot_reply = DeviceCheckAction().execute(host, udid, log_lines=log_lines)
                say(text=bot_reply, thread_ts=thread_ts)
            else:
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
            bot_reply = _handle_infra_issue(params, text, channel, thread_ts, user_id, say, client, trace_id=trace_id)

        elif intent == "note_pattern":
            bot_reply = _handle_note_pattern(params, channel, thread_ts, user_id, say)

        elif intent == "thread_monitor":
            bot_reply = _handle_thread_monitor(params, channel, thread_ts, user_id, say)

        elif intent == "stop_monitor":
            bot_reply = _handle_stop_monitor(channel, thread_ts, user_id, say)

        else:
            bot_reply = _unclear_reply(text)
            say(text=bot_reply, thread_ts=thread_ts)

        if bot_reply:
            thread_memory.add_message(channel, thread_ts, "assistant", bot_reply)

    @app.action("clarify_choice")
    def handle_clarify_choice(ack, body, client) -> None:  # noqa: ANN001
        ack()
        # Fix #1: trace_id for clarification-choice executions.
        # Previously these had no trace_id, so if an infra action was triggered via A/B/C
        # card (not a direct @mention), its log lines couldn't be correlated end-to-end.
        trace_id = uuid.uuid4().hex[:8]
        value: str = body["actions"][0]["value"]
        user_id: str = body["user"]["id"]
        channel: str = body["channel"]["id"]
        if user_id not in AUTHORIZED_USER_IDS:
            greeting = brain.generate_unauthorized_greeting("(clicked a bot button)")
            client.chat_postMessage(channel=channel, text=greeting)
            return
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", message_ts)

        try:
            clarify_id, idx_str = value.rsplit(":", 1)
            idx = int(idx_str)
        except (ValueError, TypeError):
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":warning: Invalid action format — please try again.",
            )
            return

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
        chosen = options[idx] if 0 <= idx < len(options) else {}
        intent = chosen.get("intent", "unknown")
        params = chosen.get("params", {})

        logger.info("[%s] Clarify choice: clarify_id=%s idx=%d intent=%s user=%s", trace_id, clarify_id, idx, intent, user_id)

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
            _handle_infra_issue(params, original_text, channel, thread_ts, user_id, _say, client, trace_id=trace_id)
        elif intent == "note_pattern":
            _handle_note_pattern(params, channel, thread_ts, user_id, _say)
        else:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":thinking_face: Got it — treating as: *{chosen.get('label', 'unknown')}*. Try rephrasing for a better result.",
            )

    @app.event("message")
    def handle_message_events(event: dict, say, client, body) -> None:  # noqa: ANN001
        """Handle follow-up messages in threads where the bot was previously @mentioned.

        If the bot has been tagged in a thread, subsequent messages from authorized
        users in that same thread are processed even without an @mention.
        """
        # Ignore bot messages (including infra-bot itself)
        if event.get("bot_id") or event.get("subtype"):
            return

        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts", "")
        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        ts = event.get("ts", "")

        # Skip messages that @mention the bot — those are already handled by the
        # app_mention handler (handle_mention). Processing them here too causes
        # every @mention in an active thread to fire twice → duplicate replies.
        if re.search(r'<@[A-Z0-9]+>', text):
            return

        # Only process thread replies (not top-level channel messages)
        if not thread_ts or thread_ts == ts:
            return

        # Only process if bot was previously @mentioned in this thread
        if not thread_is_active(channel, thread_ts):
            return

        # Skip empty messages
        if not text:
            return

        # Authorization check
        if user_id not in AUTHORIZED_USER_IDS:
            return

        trace_id = uuid.uuid4().hex[:8]
        logger.info("[%s] thread follow-up from %s in %s (no @mention): %.80s",
                    trace_id, user_id, channel, text)

        # Pre-check: is this message actually directed at the bot?
        # Avoids responding to human-to-human chatter in active threads.
        thread_history_pre = thread_memory.format_for_claude(channel, thread_ts)
        if not brain.is_directed_at_bot(text, thread_history_pre):
            logger.info("[%s] follow-up ignored — not directed at bot: %.80s", trace_id, text)
            return

        # Route through the same pipeline as a normal @mention
        # Re-activate to refresh TTL with every message
        activate_thread(channel, thread_ts, activated_by=user_id)

        try:
            thread_history = thread_history_pre  # reuse what we fetched for the pre-check
            thread_memory.add_message(channel, thread_ts, "user", text)

            prefetched_thread: list[dict] | None = None
            try:
                _r = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
                prefetched_thread = _r.get("messages", [])
            except Exception:  # noqa: BLE001
                prefetched_thread = []

            live_history = _build_live_thread_history(
                client, channel, thread_ts, ts, limit=10,
                prefetched=prefetched_thread,
            )
            if live_history:
                thread_history = live_history

            thread_ctx = _build_thread_context(
                client, channel, thread_ts, ts,
                prefetched=prefetched_thread,
            )

            result = brain.classify(text, thread_history)
            intent = result.get("intent", "unknown")
            params = result.get("params", {})

            logger.info("[%s] follow-up intent=%s confidence=%.2f",
                        trace_id, intent, result.get("confidence", 0))

            if intent == "_direct_reply":
                reply = params.get("reply", "")
                if reply:
                    say(text=reply, thread_ts=thread_ts)
                    thread_memory.add_message(channel, thread_ts, "assistant", reply)
                return

            if intent in ("create_jira",):
                res = _exec_create_jira(params, slack_client=client)
                say(text=_jira_created_reply(res), thread_ts=thread_ts)
                return

            if intent in ("jenkins_search", "jenkins_params", "jenkins_status"):
                _handle_infra_issue(params, text, channel, thread_ts, user_id, say, client,
                                    trace_id=trace_id)
                return

            if intent not in ("unknown", "classify"):
                _handle_infra_issue(params, text, channel, thread_ts, user_id, say, client,
                                    trace_id=trace_id)

        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] follow-up handler error: %s", trace_id, exc)
