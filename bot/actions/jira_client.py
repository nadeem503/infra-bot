"""Full-featured Jira client for infra-bot.

Capabilities:
- ADF (Atlassian Document Format) description with clickable links
- All TE-project custom fields with safe defaults
- Slack user ID → JIRA accountId resolution (via email)
- Fuzzy-match status transitions
- Ticket completeness checklist
"""
from __future__ import annotations

import base64
import re
from difflib import get_close_matches
from typing import Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

JIRA_BASE = f"https://api.atlassian.com/ex/jira/{settings.JIRA_CLOUD_ID}/rest/api/3"
JIRA_BROWSE = "https://lambdatest.atlassian.net/browse"
PROJECT_KEY = "TE"
ISSUE_TYPE_ID = "10204"          # Simple Task
TEAM_FIELD = "b79a27b6-de36-4381-8d60-0b0c3e6477a7"  # Platform Engineering

# Default values for all TE-project required custom fields
_CUSTOM_FIELD_DEFAULTS: dict = {
    "customfield_11446": {"value": "Sev 4"},                        # Bug Severity
    "customfield_10880": {"value": "No Risk"},                      # Risk Type
    "customfield_10881": {"value": "None"},                         # Customer type
    "customfield_10883": {"value": "Low"},                          # Issue Urgency
    "customfield_12127": {"value": "Consistently Reproducible"},    # Reproducibility
    "customfield_12126": [{"value": "No"}],                         # Security Risk
    "customfield_10907": ["NA"],                                    # Customer Emails
    "customfield_10840": [{"value": "KaneAI"}],                     # Impacted Products
}

# Completeness checklist keywords
_CHECKLIST = {
    "RFC":               ["rfc", "request for change"],
    "Scope":             ["scope", "in scope", "out of scope"],
    "Evidence":          ["evidence", "screenshot", "log", "video", "loom", "drive"],
    "Dev Test Cases":    ["dev test", "test case", "test cases"],
    "Automation":        ["automation", "automated", "automate"],
}

_URL_RE = re.compile(r"https?://\S+")


def _jira_headers() -> dict:
    creds = base64.b64encode(
        f"{settings.JIRA_EMAIL}:{settings.JIRA_API_TOKEN}".encode()
    ).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------

def _text_to_adf_content(text: str) -> list[dict]:
    """Convert plain text with inline URLs to ADF paragraph nodes.

    URLs are rendered as clickable ADF inlineCard nodes.
    Non-URL segments become plain text nodes.
    Multiple blank lines become separate paragraph nodes.
    """
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    content: list[dict] = []

    for para in paragraphs:
        inline_nodes: list[dict] = []
        last_end = 0
        for m in _URL_RE.finditer(para):
            before = para[last_end:m.start()]
            if before:
                inline_nodes.append({"type": "text", "text": before})
            url = m.group(0).rstrip(".,;)")
            inline_nodes.append({
                "type": "inlineCard",
                "attrs": {"url": url},
            })
            last_end = m.start() + len(url)
        tail = para[last_end:]
        if tail:
            inline_nodes.append({"type": "text", "text": tail})
        if inline_nodes:
            content.append({"type": "paragraph", "content": inline_nodes})

    if not content:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": text or " "}]})

    return content


def _build_adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": _text_to_adf_content(text),
    }


# ---------------------------------------------------------------------------
# Slack → JIRA user resolution
# ---------------------------------------------------------------------------

def resolve_slack_user_to_jira(slack_user_id: str, slack_client=None) -> Optional[str]:
    """Resolve a Slack user ID to a JIRA accountId.

    Steps:
      1. Fetch user email from Slack API (requires users:read.email scope)
      2. Look up JIRA accountId by email

    Returns accountId string or None if not found.
    """
    if not slack_user_id or not slack_client:
        return None
    try:
        resp = slack_client.users_info(user=slack_user_id)
        email = resp["user"]["profile"].get("email", "")
    except Exception as exc:
        logger.warning("Slack users_info failed for %s: %s", slack_user_id, exc)
        return None
    if not email:
        return None
    return _jira_account_by_email(email)


def _jira_account_by_email(email: str) -> Optional[str]:
    """Look up a JIRA accountId by email address."""
    try:
        resp = requests.get(
            f"{JIRA_BASE}/user/search",
            params={"query": email},
            headers=_jira_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            users = resp.json()
            if users:
                return users[0].get("accountId")
    except Exception as exc:
        logger.warning("JIRA user lookup failed for %s: %s", email, exc)
    return None


# ---------------------------------------------------------------------------
# Core create / transition / completeness
# ---------------------------------------------------------------------------

def create_issue(
    title: str,
    description: str = "",
    assignee_jira_id: Optional[str] = None,
    priority: str = "Medium",
    labels: Optional[list[str]] = None,
    custom_overrides: Optional[dict] = None,
) -> dict:
    """Create a JIRA issue in project TE with full ADF description and custom fields.

    Returns:
        {"success": True, "ticket_key": "TE-123", "url": "...", "title": "..."}
        {"success": False, "error": "..."}
    """
    if not all([settings.JIRA_EMAIL, settings.JIRA_API_TOKEN]):
        return {"success": False, "error": "Jira credentials not configured"}

    fields: dict = {
        "project": {"key": PROJECT_KEY},
        "summary": title,
        "issuetype": {"id": ISSUE_TYPE_ID},
        "customfield_10001": TEAM_FIELD,
        "description": _build_adf(description or title),
    }

    # Assignee
    assignee_id = assignee_jira_id or settings.JIRA_ASSIGNEE_ID
    if assignee_id:
        fields["assignee"] = {"id": assignee_id}

    # Priority
    if priority:
        fields["priority"] = {"name": priority}

    # Labels
    if labels:
        fields["labels"] = labels

    # Apply all custom field defaults then override
    fields.update(_CUSTOM_FIELD_DEFAULTS)
    if custom_overrides:
        fields.update(custom_overrides)

    try:
        resp = requests.post(
            f"{JIRA_BASE}/issue",
            json={"fields": fields},
            headers=_jira_headers(),
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        return {"success": False, "error": type(exc).__name__}

    if resp.status_code == 201:
        key = data.get("key", "?")
        logger.info("Jira issue created: %s", key)
        return {
            "success": True,
            "ticket_key": key,
            "url": f"{JIRA_BROWSE}/{key}",
            "title": title,
            "assignee": assignee_id,
            "priority": priority,
        }

    errors = data.get("errors") or data.get("errorMessages") or str(data)
    logger.error("Jira create failed %s: %s", resp.status_code, errors)
    return {"success": False, "error": errors}


def transition_issue(ticket_key: str, target_status: str) -> dict:
    """Transition a JIRA issue to the closest matching status.

    Uses fuzzy matching so "in progress", "In Progress", "in-progress" all work.

    Returns:
        {"success": True, "transitioned_to": "In Progress"}
        {"success": False, "error": "..."}
    """
    try:
        resp = requests.get(
            f"{JIRA_BASE}/issue/{ticket_key}/transitions",
            headers=_jira_headers(),
            timeout=10,
        )
        transitions = resp.json().get("transitions", [])
    except Exception as exc:
        return {"success": False, "error": type(exc).__name__}

    names = [t["name"] for t in transitions]
    matches = get_close_matches(target_status, names, n=1, cutoff=0.4)
    if not matches:
        # Fallback: case-insensitive substring match
        target_lower = target_status.lower().replace("-", " ")
        for name in names:
            if target_lower in name.lower():
                matches = [name]
                break

    if not matches:
        return {"success": False, "error": f"No transition matching '{target_status}'. Available: {names}"}

    chosen = matches[0]
    transition_id = next(t["id"] for t in transitions if t["name"] == chosen)

    try:
        resp = requests.post(
            f"{JIRA_BASE}/issue/{ticket_key}/transitions",
            json={"transition": {"id": transition_id}},
            headers=_jira_headers(),
            timeout=10,
        )
    except Exception as exc:
        return {"success": False, "error": type(exc).__name__}

    if resp.status_code == 204:
        logger.info("Transitioned %s → %s", ticket_key, chosen)
        return {"success": True, "transitioned_to": chosen}
    return {"success": False, "error": f"HTTP {resp.status_code}"}


def assign_issue(ticket_key: str, assignee_jira_id: str) -> dict:
    """Assign a JIRA issue to an account ID."""
    try:
        resp = requests.put(
            f"{JIRA_BASE}/issue/{ticket_key}/assignee",
            json={"accountId": assignee_jira_id},
            headers=_jira_headers(),
            timeout=10,
        )
    except Exception as exc:
        return {"success": False, "error": type(exc).__name__}
    if resp.status_code == 204:
        return {"success": True, "ticket_key": ticket_key, "assignee": assignee_jira_id}
    return {"success": False, "error": f"HTTP {resp.status_code}"}


def check_ticket_completeness(ticket_key: str) -> list[dict]:
    """Check whether a JIRA ticket description contains the standard TE checklist items.

    Returns a list of {"name": str, "filled": bool} dicts.
    """
    try:
        resp = requests.get(
            f"{JIRA_BASE}/issue/{ticket_key}?fields=description",
            headers=_jira_headers(),
            timeout=10,
        )
        desc_adf = resp.json().get("fields", {}).get("description") or {}
    except Exception as exc:
        logger.warning("Could not fetch ticket %s: %s", ticket_key, exc)
        return [{"name": k, "filled": False} for k in _CHECKLIST]

    # Flatten ADF to plain text for keyword matching
    plain = _flatten_adf(desc_adf)
    lower = plain.lower()

    results = []
    for name, keywords in _CHECKLIST.items():
        filled = any(kw in lower for kw in keywords)
        results.append({"name": name, "filled": filled})
    return results


def _flatten_adf(node: dict | list | str, _buf: list | None = None) -> str:
    """Recursively extract text from an ADF document."""
    if _buf is None:
        _buf = []
    if isinstance(node, str):
        _buf.append(node)
    elif isinstance(node, list):
        for item in node:
            _flatten_adf(item, _buf)
    elif isinstance(node, dict):
        if node.get("type") == "text":
            _buf.append(node.get("text", ""))
        for v in node.get("content", []):
            _flatten_adf(v, _buf)
    return " ".join(_buf)
