"""Natural language command parser.

Detects intent and extracts parameters from messages like:
  "create a JIRA story to generate xcresultbundle using go-ios and assign it to <@U06D6DENXQR> cc <@U067CB388G2>"
  "create a JIRA story to X and assign it to U06D6DENXQR cc U067CB388G2"  (bare IDs, no angle brackets)
  "send a meeting invite to @tejasa and @sanjays every Friday 1 PM-1:30PM IST..."
  "assign TE-534 to <@U01J2N950BB>"
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Regex primitives
# ---------------------------------------------------------------------------

# <@USERID> style mentions
MENTION = re.compile(r'<@([A-Z0-9]+)>')
# Bare uppercase Slack IDs typed without angle brackets (9-11 chars)
BARE_ID = re.compile(r'\b([A-Z][A-Z0-9]{8,10})\b')
# Jira ticket keys
TICKET_KEY = re.compile(r'\b([A-Z]+-\d+)\b')


@dataclass
class ParsedCommand:
    intent: str                            # create_jira | assign_ticket | send_invite
    params: dict = field(default_factory=dict)


def _after(text: str, keyword: str) -> str:
    """Return everything after the first case-insensitive occurrence of keyword."""
    idx = text.lower().find(keyword.lower())
    return text[idx + len(keyword):].strip() if idx >= 0 else ""


def _first_user_id(text: str) -> str:
    """Return the first Slack user ID found (mention or bare)."""
    m = MENTION.search(text) or BARE_ID.search(text)
    return m.group(1) if m else ""


def _all_user_ids(text: str) -> list[str]:
    """Return all user IDs in order (mention style preferred, then bare)."""
    ids = MENTION.findall(text)
    if not ids:
        ids = BARE_ID.findall(text)
    return ids


class CommandParser:
    """Regex-based intent classifier for on-demand manager commands."""

    def parse(self, text: str) -> Optional[ParsedCommand]:
        """Return a ParsedCommand if a known intent is detected, else None."""
        clean = self._strip_bot_mention(text)

        if re.search(r'\bcreate\s+a?\s*jira\b', clean, re.IGNORECASE):
            return self._parse_jira_create(clean)

        if re.search(r'\bsend\s+(?:a\s+)?meeting\s+invite\b', clean, re.IGNORECASE):
            return self._parse_invite(clean)

        ticket = TICKET_KEY.search(clean)
        if ticket and re.search(r'\bassign\b', clean, re.IGNORECASE):
            return self._parse_assign(clean, ticket.group(1))

        return None

    # ------------------------------------------------------------------
    # Intent parsers
    # ------------------------------------------------------------------

    def _parse_jira_create(self, text: str) -> ParsedCommand:
        # Issue type (default Task)
        tm = re.search(r'\b(story|task|bug|epic)\b', text, re.IGNORECASE)
        issue_type = tm.group(1).title() if tm else "Task"

        # Title: text between "jira [type] [to]?" and the first "assign"
        title_m = re.search(
            r'jira\s+(?:story|task|bug|epic)?\s*(?:to\s+)?(.+?)(?:\s+and\s+assign\b|\s+assign\b)',
            text, re.IGNORECASE,
        )
        title = title_m.group(1).strip() if title_m else ""

        # Assignee: first ID appearing after "assign [it] to"
        assign_tail = _after(text, "assign")
        assign_tail = re.sub(r'^it\s+to\s+|^to\s+', '', assign_tail, flags=re.IGNORECASE)
        assignee = _first_user_id(assign_tail)

        # CC: everything after the word "cc"
        cc = self._extract_cc(text)

        logger.debug("create_jira parsed: type=%s title=%r assignee=%s cc=%s",
                     issue_type, title, assignee, cc)
        return ParsedCommand(intent="create_jira", params={
            "issue_type": issue_type,
            "title": title,
            "assignee": assignee,
            "cc": cc,
        })

    def _parse_invite(self, text: str) -> ParsedCommand:
        # Attendees: all mentions/IDs before "every" or end of "invite to X and Y"
        invite_tail = _after(text, "invite to") or _after(text, "invite")
        # Split at "every" to separate attendees from schedule
        attendee_section = re.split(r'\bevery\b', invite_tail, flags=re.IGNORECASE)[0]
        attendees = _all_user_ids(attendee_section)

        freq_m = re.search(r'every\s+(\w+)', text, re.IGNORECASE)
        time_m = re.search(
            r'(\d{1,2}(?::\d{2})?\s*[AP]M)\s*[-\u2013]\s*(\d{1,2}(?::\d{2})?\s*[AP]M)',
            text, re.IGNORECASE,
        )
        agenda_m = re.search(r'agenda[:\s]+(.+?)(?:\.|ensure|$)', text, re.IGNORECASE)
        ensure_m = re.search(r'ensure\s+(.+?)(?:\.|$)', text, re.IGNORECASE)

        return ParsedCommand(intent="send_invite", params={
            "attendees": attendees,
            "frequency": freq_m.group(1) if freq_m else "",
            "time_range": f"{time_m.group(1)}-{time_m.group(2)}" if time_m else "",
            "agenda": agenda_m.group(1).strip() if agenda_m else "",
            "ensure": ensure_m.group(1).strip() if ensure_m else "",
        })

    def _parse_assign(self, text: str, ticket_key: str) -> ParsedCommand:
        assign_tail = _after(text, "assign")
        assign_tail = re.sub(r'^it\s+to\s+|^to\s+', '', assign_tail, flags=re.IGNORECASE)
        return ParsedCommand(intent="assign_ticket", params={
            "ticket_key": ticket_key,
            "assignee": _first_user_id(assign_tail),
            "cc": self._extract_cc(text),
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _strip_bot_mention(self, text: str) -> str:
        """Remove the leading <@BOTID> before parsing."""
        return re.sub(r'^<@[A-Z0-9]+>\s*', '', text).strip()

    def _extract_cc(self, text: str) -> list[str]:
        cc_m = re.search(r'\bcc[:\s]+(.+)', text, re.IGNORECASE)
        if not cc_m:
            return []
        return _all_user_ids(cc_m.group(1))
