"""Slack Home Tab dashboard.

Publishes a live dashboard every time a user opens the bot's Home tab.
Shows: pending approvals, today's stats, top issues, learned fix patterns.

Also checks for token/API key expiry on open and DMs the user if < 7 days away.
"""
from __future__ import annotations

import time
from datetime import datetime

from slack_bolt import App

from bot.approval.approval_manager import approval_manager
from bot.memory import learning_store
from bot.memory.redis_client import get_redis
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

EXPIRY_WARN_DAYS = 7


def _today_stats() -> dict:
    r = get_redis()
    date_str = time.strftime("%Y-%m-%d")
    success = int(r.hget(f"infra:stats:daily:{date_str}", "success") or 0)
    failed = int(r.hget(f"infra:stats:daily:{date_str}", "failed") or 0)
    top_raw = r.hgetall(f"infra:stats:daily:{date_str}:issues") or {}
    top = max(top_raw, key=lambda k: int(top_raw[k])) if top_raw else None
    top_count = int(top_raw[top]) if top else 0
    return {
        "total": success + failed,
        "success": success,
        "failed": failed,
        "top_issue": top,
        "top_count": top_count,
    }


def _quarantined_count() -> int:
    return sum(1 for _ in get_redis().scan_iter("infra:quarantine:*", count=100))


def _check_token_expiry() -> list[str]:
    """Return list of warning messages for tokens expiring within 7 days."""
    warnings = []
    checks = [
        ("JIRA_TOKEN_EXPIRES", "Jira API token"),
        ("JENKINS_TOKEN_EXPIRES", "Jenkins API token"),
    ]
    today = datetime.now().date()
    for attr, label in checks:
        expiry_str = getattr(settings, attr, "")
        if not expiry_str:
            continue
        try:
            expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            days_left = (expiry - today).days
            if days_left <= EXPIRY_WARN_DAYS:
                icon = ":rotating_light:" if days_left <= 1 else ":key:"
                warnings.append(
                    f"{icon} *{label}* expires in *{days_left}d* ({expiry_str}) — rotate it soon"
                )
        except ValueError:
            pass
    return warnings


def _build_home_view() -> dict:
    approval_manager.cleanup_expired()
    pending = approval_manager.list_pending()
    stats = _today_stats()
    quarantined = _quarantined_count()
    learned = learning_store.get_all_stats()[:5]
    expiry_warnings = _check_token_expiry()

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "📊 Infra-Bot Dashboard"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Pending Approvals*\n{len(pending)}"},
                {"type": "mrkdwn", "text": f"*Quarantined Devices*\n{quarantined}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Actions Today*\n{stats['total']} ({stats['success']} ✅  {stats['failed']} ❌)",
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Top Issue Today*\n"
                        + (f"`{stats['top_issue']}` ({stats['top_count']}x)" if stats["top_issue"] else "none")
                    ),
                },
            ],
        },
        {"type": "divider"},
    ]

    # Token expiry warnings
    if expiry_warnings:
        for w in expiry_warnings:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": w}})
        blocks.append({"type": "divider"})

    # Pending actions list
    if pending:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*⏳ Pending Approvals ({len(pending)}):*"},
        })
        for rec in pending[:8]:
            age = max(0, int((time.time() - rec.requested_at) / 60))
            devices_str = ", ".join(f"`{d}`" for d in rec.devices[:2])
            if len(rec.devices) > 2:
                devices_str += f" +{len(rec.devices) - 2}"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"• `{rec.action_id}` — `{rec.action_type}` | "
                        f"{rec.region} | {devices_str or '_no device_'} | "
                        f"{age}m ago | <@{rec.requested_by}>"
                    ),
                },
            })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":white_check_mark: *No pending actions*"},
        })

    # Learned fix patterns
    if learned:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🧠 Learned Fix Patterns:*"},
        })
        for p in learned:
            pct = int(p["success_rate"] * 100)
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"• `{p['issue_type']}` in `{p['region']}` → `{p['action_type']}` "
                        f"({pct}% success over {p['total']} runs)"
                    ),
                },
            })

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"_Last refreshed: {time.strftime('%Y-%m-%d %H:%M:%S UTC')} • Open tab to refresh_"},
        ],
    })

    return {"type": "home", "blocks": blocks}


def register_home_tab_listener(app: App) -> None:
    @app.event("app_home_opened")
    def handle_home_opened(event: dict, client) -> None:
        user_id = event.get("user", "")
        try:
            view = _build_home_view()
            client.views_publish(user_id=user_id, view=view)

            # DM expiry warnings to the user
            expiry_warnings = _check_token_expiry()
            if expiry_warnings and user_id == settings.APPROVER_SLACK_ID:
                for warning in expiry_warnings:
                    client.chat_postMessage(channel=user_id, text=warning)

        except Exception as exc:  # noqa: BLE001
            logger.error("Home tab error for user %s: %s", user_id, exc)
