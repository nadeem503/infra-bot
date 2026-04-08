"""Claude AI brain for infra-bot.

Uses Claude to:
  1. Classify intent from natural language (with thread context for follow-ups)
  2. Extract structured parameters
  3. Generate casual human-sounding responses
  4. Generate clarification options when confidence is low
  5. Analyze correlated signals for root cause diagnosis
"""
from __future__ import annotations

import json
from typing import Optional

import anthropic

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = """
You are the brain of Infra-Bot, an infrastructure assistant for LambdaTest's Device Cloud team.

Your job: read a Slack message (and optional prior thread context) and return a JSON object
classifying the intent and extracting parameters.

Supported intents:
- create_jira      — create a new Jira ticket in project TE
- assign_ticket    — assign an existing Jira ticket to someone
- send_invite      — send a calendar/meeting invite
- infra_issue      — infrastructure problem (device down, reboot, ADB, network, DB, Jenkins, crash, storage, device_disconnected)
- unknown          — cannot determine

Always respond with ONLY valid JSON, no explanation, no markdown fences:
{
  "intent": "<intent>",
  "confidence": 0.0-1.0,
  "params": {
    // create_jira
    "title": "ticket summary",
    "issue_type": "Story" | "Task" | "Bug",
    "assignee": "SLACK_USER_ID or empty",
    "cc": ["SLACK_USER_ID"],

    // assign_ticket
    "ticket_key": "TE-XXX",
    "assignee": "SLACK_USER_ID",
    "cc": ["SLACK_USER_ID"],

    // send_invite
    "attendees": ["SLACK_USER_ID"],
    "frequency": "Friday",
    "time_range": "1 PM-1:30 PM",
    "timezone": "IST",
    "agenda": "...",

    // infra_issue
    "issue_category": "device_down" | "reboot" | "adb_issue" | "network_issue" | "db_mismatch" | "jenkins_failure" | "app_crash" | "storage_issue" | "device_disconnected",
    "devices": ["udid or ip or hostname"],
    "region": "india" | "us" | "dublin" | "ap" | null
  }
}

Rules:
- Slack user IDs: 11-char strings starting with U — extract the ID from <@U...> format
- UDIDs: 40-char hex strings
- Device IPs: 10.151.x.x -> region ap, 10.100.x.x -> dublin, 10.146.x.x -> us
- "MISMATCH: DB=N, Device=device not found" -> intent=infra_issue, issue_category=device_disconnected
- If follow-up context is present, use it to fill missing params
- If no assignee mentioned, use empty string; if no cc, use empty array
"""

RESPOND_SYSTEM = """
You are Infra-Bot, a friendly infrastructure assistant on Slack for LambdaTest.
Generate a short, casual Slack reply based on what just happened.

Style rules:
- Sound like a helpful human colleague, not a system
- Use :white_check_mark: for success, :x: for errors, :thinking_face: for unclear
- Start completions with "Done :white_check_mark:"
- For Jira created: "Done :white_check_mark:\nCreated <URL|KEY> — _title_\nAssigned to <@ID>\ncc: <@ID>"
- Keep it 2-4 lines max
- Vary phrasing each time so it doesn't feel copy-pasted
- Never say "I have successfully" — just confirm naturally
"""

CLARIFY_SYSTEM = """
You are Infra-Bot. A Slack message was unclear (low classification confidence).
Suggest exactly 3 possible interpretations as a JSON array.

Each item must have:
  {"label": "Short button label (max 5 words)", "intent": "<intent>", "params": {...}}

Intents: create_jira | assign_ticket | send_invite | infra_issue | unknown

Return ONLY a valid JSON array, no explanation, no markdown.
"""

ROOT_CAUSE_SYSTEM = """
You are Infra-Bot analyzing correlated infrastructure signals.
Multiple issues were reported in quick succession from the same channel/region.

Analyze the signals and produce a concise Slack-formatted root cause hypothesis.
Format:
  :mag: *Root Cause Analysis*
  • *Likely cause:* <one-line hypothesis>
  • *Evidence:* <what signals support this>
  • *Recommended action:* <single best next step>
Keep it under 6 lines. Use :rotating_light: if it looks like a network or rack-level incident.
"""


class ClaudeBrain:
    """Thin wrapper around Anthropic SDK."""

    def __init__(self) -> None:
        self._client: Optional[anthropic.Anthropic] = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not settings.ANTHROPIC_API_KEY:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        return self._client

    def classify(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Classify intent + extract params. thread_history enables follow-up context."""
        try:
            messages: list[dict] = []
            if thread_history:
                messages.extend(thread_history)
            messages.append({"role": "user", "content": text})

            resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=CLASSIFY_SYSTEM,
                messages=messages,
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            result = json.loads(raw)
            logger.debug("Classified: intent=%s confidence=%.2f", result.get("intent"), result.get("confidence", 0))
            return result
        except json.JSONDecodeError as exc:
            logger.error("Claude returned invalid JSON: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Claude classify error: %s", exc)
        return {"intent": "unknown", "params": {}, "confidence": 0.0}

    def clarification_options(self, text: str) -> list[dict]:
        """Return 3 possible interpretations for a low-confidence message."""
        try:
            resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=CLARIFY_SYSTEM,
                messages=[{"role": "user", "content": text}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            options = json.loads(raw)
            return options[:3] if isinstance(options, list) else []
        except Exception as exc:  # noqa: BLE001
            logger.error("clarification_options error: %s", exc)
            return [
                {"label": "Check device status", "intent": "infra_issue", "params": {"issue_category": "device_down"}},
                {"label": "Create Jira ticket", "intent": "create_jira", "params": {}},
                {"label": "Something else", "intent": "unknown", "params": {}},
            ]

    def analyze_root_cause(self, signals_text: str) -> str:
        """Produce grouped root cause diagnosis from correlated signals."""
        try:
            resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=ROOT_CAUSE_SYSTEM,
                messages=[{"role": "user", "content": f"Signals:\n{signals_text}"}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("analyze_root_cause error: %s", exc)
            return ":mag: *Root Cause Analysis*\n• Multiple correlated signals detected — manual investigation recommended"

    def generate_response(self, action: str, context: dict) -> str:
        """Generate a casual Slack reply for a completed action."""
        try:
            prompt = f"Action just taken: {action}\nContext: {json.dumps(context, default=str)}"
            resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=RESPOND_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("generate_response error: %s", exc)
            return "Done :white_check_mark:" if context.get("success") else ":x: Something went wrong."


# Module-level singleton
brain = ClaudeBrain()
