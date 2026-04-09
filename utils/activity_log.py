"""Structured activity logger for infra-bot.

Writes JSONL to logs/activity.jsonl. Three event types:
  - claude_call   : every Claude CLI subprocess invocation
  - user_request  : every @mention received from Slack users
  - bot_session   : bot process start/stop + Slack socket connect/disconnect

Read back via slash command: /infra logs claude|users|sessions
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

_LOG_PATH = Path("logs/activity.jsonl")
_lock = threading.Lock()


def _write(entry: dict) -> None:
    """Append a JSONL entry. Thread-safe."""
    entry.setdefault("ts", time.time())
    line = json.dumps(entry, separators=(",", ":"))
    with _lock:
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass  # never crash the bot over logging


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def log_claude_call(
    prompt_preview: str,
    response_preview: str,
    duration_ms: int,
    success: bool,
    action: str = "",       # "classify" | "direct" | "rca"
    intent: str = "",       # classified intent if action=classify
    error: str = "",
) -> None:
    _write({
        "type": "claude_call",
        "success": success,
        "action": action,
        "intent": intent,
        "duration_ms": duration_ms,
        "prompt_preview": prompt_preview[:120],
        "response_preview": response_preview[:200],
        "error": error[:200] if error else "",
    })


def log_user_request(
    user_id: str,
    channel: str,
    text_preview: str,
    intent: str,
    confidence: float,
    source: str,            # "local" | "claude" | "gemini"
) -> None:
    _write({
        "type": "user_request",
        "user_id": user_id,
        "channel": channel,
        "text_preview": text_preview[:150],
        "intent": intent,
        "confidence": round(confidence, 2),
        "source": source,
    })


def log_bot_session(event: str, session_id: str = "", pid: int = 0) -> None:
    """event: 'start' | 'stop' | 'connected' | 'disconnected'"""
    _write({
        "type": "bot_session",
        "event": event,
        "session_id": session_id,
        "pid": pid or os.getpid(),
    })


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _read_recent(event_type: str, hours: float = 24, limit: int = 100) -> list[dict]:
    """Return recent entries of a given type, newest first."""
    if not _LOG_PATH.exists():
        return []
    cutoff = time.time() - hours * 3600
    entries: list[dict] = []
    try:
        with _LOG_PATH.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("type") == event_type and e.get("ts", 0) >= cutoff:
                        entries.append(e)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return list(reversed(entries[-limit:]))


def get_claude_calls(hours: float = 24, limit: int = 50) -> list[dict]:
    return _read_recent("claude_call", hours, limit)


def get_user_requests(hours: float = 24, limit: int = 50) -> list[dict]:
    return _read_recent("user_request", hours, limit)


def get_bot_sessions(hours: float = 72, limit: int = 30) -> list[dict]:
    return _read_recent("bot_session", hours, limit)


def get_claude_stats(hours: float = 24) -> dict:
    calls = get_claude_calls(hours, limit=500)
    total = len(calls)
    ok = sum(1 for c in calls if c.get("success"))
    fail = total - ok
    avg_ms = int(sum(c.get("duration_ms", 0) for c in calls) / total) if total else 0
    direct = sum(1 for c in calls if c.get("action") == "direct")
    classify = sum(1 for c in calls if c.get("action") == "classify")
    rca = sum(1 for c in calls if c.get("action") == "rca")
    return {
        "total": total, "ok": ok, "fail": fail,
        "avg_ms": avg_ms, "direct": direct,
        "classify": classify, "rca": rca,
    }


def get_user_stats(hours: float = 24) -> dict:
    reqs = get_user_requests(hours, limit=500)
    total = len(reqs)
    by_source: dict[str, int] = {}
    by_intent: dict[str, int] = {}
    users: set[str] = set()
    for r in reqs:
        by_source[r.get("source", "?")] = by_source.get(r.get("source", "?"), 0) + 1
        by_intent[r.get("intent", "?")] = by_intent.get(r.get("intent", "?"), 0) + 1
        users.add(r.get("user_id", ""))
    return {
        "total": total, "unique_users": len(users),
        "by_source": by_source, "by_intent": by_intent,
    }
