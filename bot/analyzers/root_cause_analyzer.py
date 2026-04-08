"""Root cause chain analyzer.

Tracks issue signals per channel within a 10-minute window.
When 3+ signals with 2+ distinct issue types appear, triggers Claude to
produce a single grouped root cause diagnosis instead of separate approval flows.

Example: 3 devices down + ADB offline + Jenkins failing in AP
  → "Likely network partition affecting AP rack, not individual device faults"
"""
from __future__ import annotations

import json
import time

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 600    # 10-minute signal window
MIN_SIGNALS = 3         # signals before triggering analysis
MIN_ISSUE_TYPES = 2     # distinct issue types needed for correlation
_PREFIX = "infra:signals"
_ANALYZED_TTL = 300     # prevent re-analysis for 5 min after posting


def add_signal(
    channel: str,
    issue_type: str,
    device: str,
    region: str,
    user_id: str,
) -> list[dict]:
    """Append a signal and return all signals in the current window."""
    r = get_redis()
    key = f"{_PREFIX}:{channel}"
    entry = json.dumps({
        "issue_type": issue_type,
        "device": device,
        "region": region,
        "user_id": user_id,
        "ts": time.time(),
    })
    r.rpush(key, entry)
    r.expire(key, WINDOW_SECONDS)

    cutoff = time.time() - WINDOW_SECONDS
    all_raw = r.lrange(key, 0, -1)
    return [
        json.loads(e) for e in all_raw
        if json.loads(e).get("ts", 0) >= cutoff
    ]


def should_correlate(signals: list[dict]) -> bool:
    """True when signals are diverse enough to warrant a grouped diagnosis."""
    if len(signals) < MIN_SIGNALS:
        return False
    issue_types = {s["issue_type"] for s in signals}
    return len(issue_types) >= MIN_ISSUE_TYPES


def already_analyzed(channel: str) -> bool:
    return bool(get_redis().exists(f"{_PREFIX}:{channel}:analyzed"))


def mark_analyzed(channel: str) -> None:
    r = get_redis()
    r.setex(f"{_PREFIX}:{channel}:analyzed", _ANALYZED_TTL, "1")
    r.delete(f"{_PREFIX}:{channel}")   # clear signals after analysis posted


def format_signals_for_claude(signals: list[dict]) -> str:
    lines = [f"- {s['issue_type']} on device `{s['device'] or 'unknown'}` in {s['region']}" for s in signals]
    return "\n".join(lines)
