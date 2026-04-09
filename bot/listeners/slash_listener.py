"""Slash command handler for /infra.

Supported subcommands:
  /infra status <ip_or_udid>            -- fetch live device health
  /infra pending                         -- list all pending approval actions
  /infra history device=<id> last=24h   -- recent audit log entries (with Replay button)
  /infra faulty count                    -- count offline/faulty devices from DB (read-only)

Note: Register the /infra slash command in your Slack App settings
(Features > Slash Commands) pointing to your Socket Mode app.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from slack_bolt import App

from bot.approval.approval_manager import approval_manager
from bot.formatters.slack_formatter import SlackFormatter
from bot.memory.redis_client import get_redis
from config import settings
from utils.activity_log import (
    get_claude_calls, get_user_requests, get_bot_sessions,
    get_claude_stats, get_user_stats,
)
from utils.device_name import get_device_name
from utils.logger import get_logger

logger = get_logger(__name__)
_formatter = SlackFormatter()

LOGS_PATH = Path("logs/actions.jsonl")
REPLAY_TTL = 3600   # 1 hour

FAULTY_QUERY = (
    "SELECT COUNT(*) AS faulty_count FROM devices "
    "WHERE status IN ('offline', 'faulty', 'error', 'down')"
)


def _parse(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    return (parts[0].lower() if parts else "help"), parts[1:]


def _handle_status(target: str, respond) -> None:  # noqa: ANN001
    if not target:
        respond(":warning: Usage: `/infra status <ip_or_udid>`")
        return
    label = get_device_name(target)
    respond(f":hourglass: Fetching status for `{label}`...")
    try:
        from bot.actions.device_status import DeviceStatusAction  # noqa: PLC0415
        action = DeviceStatusAction(
            params={"host": target, "udid": target, "devices": [target]},
            triggered_by="slash_cmd", channel="slash", region="unknown",
        )
        result = action.run()
        icon = ":white_check_mark:" if result.get("success") else ":x:"
        msg = result.get("message", "No response")
        output = result.get("details", {}).get("output", "")
        output_str = f"\n```{output[:600]}```" if output else ""
        respond(f"{icon} *Device:* `{label}`\n{msg}{output_str}")
    except Exception as exc:  # noqa: BLE001
        respond(f":warning: Failed to fetch status: `{type(exc).__name__}: {exc}`")


def _handle_pending(respond) -> None:  # noqa: ANN001
    approval_manager.cleanup_expired()
    records = approval_manager.list_pending()
    respond(_formatter.format_pending_list(records))


def _handle_history(args: list[str], channel: str, respond) -> None:  # noqa: ANN001
    device_id = ""
    hours = 24
    for arg in args:
        if arg.startswith("device="):
            device_id = arg.split("=", 1)[1]
        elif arg.startswith("last="):
            m = re.match(r"(\d+)([hd]?)", arg.split("=", 1)[1])
            if m:
                n, unit = int(m.group(1)), m.group(2)
                hours = n * 24 if unit == "d" else n

    if not device_id:
        respond(":warning: Usage: `/infra history device=<udid_or_ip> last=24h`")
        return

    cutoff = time.time() - hours * 3600
    entries = []
    if LOGS_PATH.exists():
        with LOGS_PATH.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    devs = entry.get("devices") or []
                    if device_id in devs and entry.get("timestamp", 0) >= cutoff:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue

    label = get_device_name(device_id)
    if not entries:
        respond(f":mag: No history found for `{label}` in the last {hours}h")
        return

    # Build blocks with Replay buttons
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":scroll: *History for `{label}` (last {hours}h) — {len(entries)} entries:*",
            },
        },
        {"type": "divider"},
    ]

    for e in entries[-15:]:   # cap at 15 to stay under Slack block limit
        ts_str = time.strftime("%m/%d %H:%M", time.localtime(e.get("timestamp", 0)))
        ok = ":white_check_mark:" if e.get("status") == "completed" else ":x:"
        action_type = e.get("action_type", "unknown")
        region = e.get("region", "?")
        triggered_by = e.get("triggered_by", "?")

        # Store replay data in Redis
        replay_key = f"infra:replay:{uuid.uuid4().hex[:8]}"
        get_redis().setex(
            replay_key,
            REPLAY_TTL,
            json.dumps({
                "action_type": action_type,
                "params": e.get("params", {}),
                "region": region,
                "devices": e.get("devices", []),
            }),
        )

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{ok} `{ts_str}` — `{action_type}` | "
                    f"{region} | {e.get('status')} | by <@{triggered_by}>"
                ),
            },
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "\U0001f501 Replay"},
                "action_id": "replay_action",
                "value": replay_key,
            },
        })

    respond(blocks=blocks, text=f"History for {label}")


def _handle_faulty_count(respond) -> None:  # noqa: ANN001
    if not all([settings.DB_HOST, settings.DB_USER, settings.DB_PASSWORD, settings.DB_NAME]):
        respond(":warning: Database not configured — set DB_HOST, DB_USER, DB_PASSWORD, DB_NAME in .env")
        return
    respond(":hourglass: Querying DB for faulty device count...")
    try:
        import pymysql  # noqa: PLC0415
        conn = pymysql.connect(
            host=settings.DB_HOST, user=settings.DB_USER, password=settings.DB_PASSWORD,
            database=settings.DB_NAME, port=settings.DB_PORT, connect_timeout=10,
        )
        with conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(FAULTY_QUERY)
                row = cur.fetchone()
        count = row.get("faulty_count", 0) if row else 0
        respond(
            f":warning: *Faulty device count:* `{count}` device(s) currently offline / faulty / down\n"
            f"_Query: `{FAULTY_QUERY}`_"
        )
    except Exception as exc:  # noqa: BLE001
        respond(f":x: DB query failed: `{type(exc).__name__}: {exc}`")


def _handle_logs(args: list[str], respond) -> None:  # noqa: ANN001
    """Show recent activity logs: /infra logs [claude|users|sessions] [last=Nh]"""
    subtype = args[0].lower() if args else "claude"
    hours = 24.0
    for a in args:
        if a.startswith("last="):
            m = re.match(r"(\d+)([hd]?)", a.split("=", 1)[1])
            if m:
                n, unit = int(m.group(1)), m.group(2)
                hours = float(n * 24 if unit == "d" else n)

    ts_fmt = lambda ts: time.strftime("%m/%d %H:%M:%S", time.localtime(ts))  # noqa: E731

    if subtype == "claude":
        stats = get_claude_stats(hours)
        calls = get_claude_calls(hours, limit=20)
        ok_pct = int(stats["ok"] / stats["total"] * 100) if stats["total"] else 0
        header = (
            f":robot_face: *Claude CLI Activity* (last {int(hours)}h)\n"
            f"*Total:* {stats['total']}  •  "
            f":white_check_mark: {stats['ok']} ({ok_pct}%)  •  "
            f":x: {stats['fail']}  •  "
            f"*Avg:* {stats['avg_ms']}ms\n"
            f"*Breakdown:* classify={stats['classify']}  direct={stats['direct']}  rca={stats['rca']}"
        )
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "divider"},
        ]
        for c in calls[:15]:
            icon = ":white_check_mark:" if c.get("success") else ":x:"
            action = c.get("action", "?")
            intent = f" → `{c['intent']}`" if c.get("intent") else ""
            dur = c.get("duration_ms", 0)
            err = f"\n_Error: {c['error'][:80]}_" if c.get("error") else ""
            preview = c.get("response_preview", "")[:80].replace("\n", " ")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{icon} `{ts_fmt(c['ts'])}` `{action}`{intent} "
                        f"_{dur}ms_\n> {preview}{err}"
                    ),
                },
            })
        respond(blocks=blocks, text="Claude CLI logs")

    elif subtype in ("users", "requests"):
        stats = get_user_stats(hours)
        reqs = get_user_requests(hours, limit=20)
        source_str = "  ".join(f"{k}={v}" for k, v in stats["by_source"].items())
        intent_str = "  ".join(
            f"`{k}`={v}" for k, v in
            sorted(stats["by_intent"].items(), key=lambda x: -x[1])[:6]
        )
        header = (
            f":speech_balloon: *User Requests* (last {int(hours)}h)\n"
            f"*Total:* {stats['total']}  •  *Unique users:* {stats['unique_users']}\n"
            f"*Sources:* {source_str}\n"
            f"*Top intents:* {intent_str}"
        )
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "divider"},
        ]
        for r in reqs[:15]:
            src_icon = {"local": ":zap:", "claude": ":robot_face:", "gemini": ":sparkles:"}.get(
                r.get("source", ""), ":grey_question:"
            )
            conf = int(r.get("confidence", 0) * 100)
            preview = r.get("text_preview", "")[:80].replace("\n", " ")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{src_icon} `{ts_fmt(r['ts'])}` <@{r.get('user_id','')}> "
                        f"→ `{r.get('intent','?')}` ({conf}%)\n> {preview}"
                    ),
                },
            })
        respond(blocks=blocks, text="User request logs")

    elif subtype == "sessions":
        sessions = get_bot_sessions(hours=72, limit=30)
        lines = [f":electric_plug: *Bot Sessions* (last 72h) — {len(sessions)} events\n"]
        for s in sessions[:25]:
            ev = s.get("event", "?")
            icon = {
                "start": ":rocket:", "stop": ":octagonal_sign:",
                "connected": ":green_circle:", "disconnected": ":red_circle:",
            }.get(ev, ":grey_question:")
            sid = s.get("session_id", "")
            sid_str = f" `{sid[:12]}…`" if sid else ""
            lines.append(f"{icon} `{ts_fmt(s['ts'])}` *{ev}*{sid_str} PID={s.get('pid','?')}")
        respond(text="\n".join(lines))

    else:
        respond(
            ":robot_face: *Usage:* `/infra logs [claude|users|sessions] [last=Nh]`\n"
            "• `claude` — Claude CLI call history & stats\n"
            "• `users` — inbound user requests & intents\n"
            "• `sessions` — bot process & Slack socket events"
        )


def register_slash_listeners(app: App) -> None:
    @app.command("/infra")
    def handle_infra(ack, body, respond) -> None:  # noqa: ANN001
        ack()
        text = body.get("text", "").strip()
        user_id = body.get("user_id", "")
        channel = body.get("channel_id", "")
        logger.info("/infra command from %s: %s", user_id, text)

        subcommand, args = _parse(text)

        if subcommand == "status" and args:
            _handle_status(args[0], respond)
        elif subcommand == "pending":
            _handle_pending(respond)
        elif subcommand == "history":
            _handle_history(args, channel, respond)
        elif subcommand == "faulty" and args and args[0] == "count":
            _handle_faulty_count(respond)
        elif subcommand == "logs":
            _handle_logs(args, respond)
        else:
            respond(
                ":robot_face: *Infra-Bot slash commands:*\n"
                "\u2022 `/infra status <ip_or_udid>` \u2014 live device health\n"
                "\u2022 `/infra pending` \u2014 list pending approvals\n"
                "\u2022 `/infra history device=<id> last=24h` \u2014 action history with Replay\n"
                "\u2022 `/infra faulty count` \u2014 count offline/faulty devices from DB\n"
                "\u2022 `/infra logs [claude|users|sessions] [last=Nh]` \u2014 activity logs & stats"
            )
