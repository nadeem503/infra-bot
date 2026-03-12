"""Claude AI brain for infra-bot.

Uses Claude to:
  1. Classify intent from any natural language Slack message (with thread context)
  2. Extract structured parameters (ticket title, assignee, devices, region, etc.)
  3. Generate casual, human-sounding responses

Using claude-haiku for fast classification (~1s latency).
"""
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
- create_jira     — create a new Jira ticket in project TE
- assign_ticket   — assign an existing Jira ticket to someone
- send_invite     — send a calendar/meeting invite
- infra_issue     — infrastructure problem (device down, reboot, ADB, network, DB, Jenkins, crash, storage)
- unknown         — cannot determine

Always respond with ONLY valid JSON, no explanation, no markdown fences:
{
  "intent": "<intent>",
  "confidence": 0.0-1.0,
  "params": {

    // create_jira
    "title": "ticket summary text",
    "issue_type": "Story" | "Task" | "Bug",
    "assignee": "SLACK_USER_ID or empty string",
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
    "issue_category": "device_down" | "reboot" | "adb_issue" | "network_issue" | "db_mismatch" | "jenkins_failure" | "app_crash" | "storage_issue",
    "devices": ["udid or ip or hostname"],
    "region": "india" | "us" | "dublin" | "ap" | null
  }
}

Rules:
- Slack user IDs: 11-char strings starting with U (e.g. U06D6DENXQR) or wrapped as <@U06D6DENXQR> — extract the ID
- UDIDs: 40-char hex strings
- Device IPs: 10.151.x.x -> region ap, 10.100.x.x -> dublin, 10.146.x.x -> us
- If follow-up context is present (prior thread messages), use it to fill in missing params
- If no assignee is mentioned, use empty string
- If no cc is mentioned, use empty array
"""

RESPOND_SYSTEM = """
You are Infra-Bot, a friendly infrastructure assistant on Slack for LambdaTest.
Generate a short, casual Slack reply based on what just happened.

Style rules:
- Sound like a helpful human colleague, not a system
- Use :white_check_mark: for success, :x: for errors, :thinking_face: for unclear
- Start completions with "Done :white_check_mark:"
- For Jira created: "Done :white_check_mark:\nCreated <URL|KEY> \u2014 _title_\nAssigned to <@ID>\ncc: <@ID>"
- Keep it 2-4 lines max
- Vary phrasing slightly each time so it doesn't feel copy-pasted
- Never say "I have successfully" — just confirm naturally
"""


# ---------------------------------------------------------------------------
# Brain class
# ---------------------------------------------------------------------------

class ClaudeBrain:
    """Thin wrapper around Anthropic SDK for classification and response generation."""

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
        """Classify intent and extract params from a Slack message.

        thread_history: prior messages as [{role, content}] for follow-up context.
        Allows Claude to understand "also reboot it" by referencing earlier mentions.
        Returns dict with keys: intent, confidence, params.
        Falls back to {intent: unknown} on any error.
        """
        try:
            messages: list[dict] = []
            if thread_history:
                # Inject prior thread turns so Claude understands follow-ups
                messages.extend(thread_history)
            messages.append({"role": "user", "content": text})

            resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=CLASSIFY_SYSTEM,
                messages=messages,
            )
            raw = resp.content[0].text.strip()

            # Strip accidental markdown code fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            result = json.loads(raw)
            logger.debug("Claude classified: intent=%s confidence=%.2f",
                         result.get("intent"), result.get("confidence", 0))
            return result

        except json.JSONDecodeError as exc:
            logger.error("Claude returned invalid JSON: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Claude classify error: %s", exc)

        return {"intent": "unknown", "params": {}, "confidence": 0.0}

    def generate_response(self, action: str, context: dict) -> str:
        """Generate a casual Slack response for a completed action.

        action: human-readable description of what was done
        context: dict with result data (ticket key, url, assignee, etc.)
        """
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
            logger.error("Claude generate_response error: %s", exc)
            return "Done :white_check_mark:" if context.get("success") else ":x: Something went wrong."


# Module-level singleton
brain = ClaudeBrain()
