"""AI brain for infra-bot — powered by Google Gemini (google-genai SDK).

Free tier: 1,500 requests/day, 15 req/min — free, no credit card needed.
Get key at: https://aistudio.google.com/apikey

Uses google-genai (the new official SDK, replaces deprecated google-generativeai).
"""
from __future__ import annotations

import json
from typing import Optional

from google import genai
from google.genai import types

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "gemini-2.0-flash"

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = """
You are the brain of Infra-Bot, an infrastructure assistant for LambdaTest's Device Cloud team.

Read the Slack message and return a JSON object classifying the intent and extracting parameters.

Supported intents:
- create_jira      — create a new Jira ticket in project TE
- assign_ticket    — assign an existing Jira ticket to someone
- send_invite      — send a calendar/meeting invite
- infra_issue      — infrastructure problem (device down, reboot, ADB, network, DB, Jenkins, crash, storage, device_disconnected)
- unknown          — cannot determine

Return ONLY this JSON structure:
{
  "intent": "<intent>",
  "confidence": 0.0-1.0,
  "params": {
    "title": "ticket summary (create_jira)",
    "issue_type": "Story|Task|Bug",
    "assignee": "SLACK_USER_ID or empty string",
    "cc": ["SLACK_USER_ID"],
    "ticket_key": "TE-XXX (assign_ticket)",
    "attendees": ["SLACK_USER_ID"],
    "frequency": "Friday",
    "time_range": "1 PM-1:30 PM",
    "timezone": "IST",
    "agenda": "...",
    "issue_category": "device_down|reboot|adb_issue|network_issue|db_mismatch|jenkins_failure|app_crash|storage_issue|device_disconnected",
    "devices": ["udid or ip or hostname"],
    "region": "india|us|dublin|ap|null"
  }
}

Rules:
- Slack user IDs: 11-char strings starting with U — extract from <@U...> format
- UDIDs: 40-char hex strings
- IPs: 10.151.x.x → region ap, 10.100.x.x → dublin, 10.146.x.x → us
- "MISMATCH: DB=N, Device=device not found" → intent=infra_issue, issue_category=device_disconnected
- Use thread context to fill missing params in follow-up messages
- Empty string for missing assignee, empty array for missing cc
"""

RESPOND_SYSTEM = """
You are Infra-Bot, a friendly infrastructure assistant on Slack for LambdaTest.
Generate a short, casual Slack reply (2-4 lines max) for what just happened.

Rules:
- Sound like a helpful human colleague, not a robot
- Use :white_check_mark: for success, :x: for errors, :thinking_face: for unclear
- Start success replies with "Done :white_check_mark:"
- For Jira created: "Done :white_check_mark:\nCreated <URL|KEY> — _title_\nAssigned to <@ID>"
- Vary phrasing each time — never copy-paste feel
- Never say "I have successfully" — just confirm naturally
"""

CLARIFY_SYSTEM = """
You are Infra-Bot. A Slack message was unclear (low confidence classification).
Suggest exactly 3 possible interpretations as a JSON array.

Each item: {"label": "Short button label (max 5 words)", "intent": "<intent>", "params": {}}

Valid intents: create_jira | assign_ticket | send_invite | infra_issue | unknown

Return ONLY a valid JSON array.
"""

ROOT_CAUSE_SYSTEM = """
You are Infra-Bot analyzing correlated infrastructure signals from the same Slack channel.
Multiple issues arrived in quick succession.

Produce a concise Slack-formatted root cause hypothesis:
  :mag: *Root Cause Analysis*
  • *Likely cause:* <one-line hypothesis>
  • *Evidence:* <which signals support this>
  • *Recommended action:* <single best next step>

Keep it under 6 lines. Use :rotating_light: for network/rack-level incidents.
"""


class AIBrain:
    """Gemini-powered intelligence for infra-bot."""

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise RuntimeError(
                    "GEMINI_API_KEY is not set — get a free key at https://aistudio.google.com/apikey"
                )
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    def _build_contents(
        self, text: str, thread_history: list[dict] | None = None
    ) -> list[types.Content]:
        """Build Gemini contents list, prepending thread history for context."""
        contents: list[types.Content] = []
        for msg in (thread_history or []):
            role = "user" if msg.get("role") == "user" else "model"
            contents.append(
                types.Content(role=role, parts=[types.Part(text=msg["content"])])
            )
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        return contents

    def classify(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Classify intent + extract params. thread_history enables follow-up context."""
        try:
            contents = self._build_contents(text, thread_history)
            resp = self.client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=CLASSIFY_SYSTEM,
                    response_mime_type="application/json",
                    max_output_tokens=512,
                ),
            )
            result = json.loads(resp.text)
            logger.debug(
                "Classified: intent=%s confidence=%.2f",
                result.get("intent"), result.get("confidence", 0),
            )
            return result
        except json.JSONDecodeError as exc:
            logger.error("Gemini returned invalid JSON: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Gemini classify error: %s", exc)
        return {"intent": "unknown", "params": {}, "confidence": 0.0}

    def clarification_options(self, text: str) -> list[dict]:
        """Return 3 possible interpretations for a low-confidence message."""
        try:
            resp = self.client.models.generate_content(
                model=MODEL,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=CLARIFY_SYSTEM,
                    response_mime_type="application/json",
                    max_output_tokens=512,
                ),
            )
            options = json.loads(resp.text)
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
            resp = self.client.models.generate_content(
                model=MODEL,
                contents=f"Signals:\n{signals_text}",
                config=types.GenerateContentConfig(
                    system_instruction=ROOT_CAUSE_SYSTEM,
                    max_output_tokens=400,
                ),
            )
            return resp.text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("analyze_root_cause error: %s", exc)
            return ":mag: *Root Cause Analysis*\n• Multiple correlated signals detected — manual investigation recommended"

    def generate_response(self, action: str, context: dict) -> str:
        """Generate a casual Slack reply for a completed action."""
        try:
            prompt = f"Action just taken: {action}\nContext: {json.dumps(context, default=str)}"
            resp = self.client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=RESPOND_SYSTEM,
                    max_output_tokens=256,
                    temperature=0.7,
                ),
            )
            return resp.text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("generate_response error: %s", exc)
            return "Done :white_check_mark:" if context.get("success") else ":x: Something went wrong."


# Module-level singleton — same name, all imports unchanged
brain = AIBrain()
