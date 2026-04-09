"""AI brain for infra-bot — Gemini primary, OpenAI fallback.

Flow: local_classifier → Gemini → OpenAI (on Gemini quota exhaustion).
Gemini free tier: 1,500 req/day.  OpenAI gpt-4o-mini: pay-as-you-go.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from google import genai
from google.genai import types

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_QUOTA_MSG = (
    ":hourglass: Both Gemini and OpenAI are unavailable right now. "
    "Try again in a moment."
)

MODEL = "gemini-2.0-flash"

# Cache TTL: 5 min for classify, 10 min for root cause
_CLASSIFY_CACHE_TTL = 300
_ROOT_CAUSE_CACHE_TTL = 600

# ---------------------------------------------------------------------------
# System prompts — kept MINIMAL (local_classifier handles common cases)
# Gemini only sees ambiguous messages that local rules couldn't handle.
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = """
You are Infra-Bot for LambdaTest's Real Device Cloud. Classify the intent of a Slack message.

Host context:
- macOS: iOS devices — services: LRR, Resigner (port 6789), IHM, LRP, Reconciler (launchctl plists)
- Ubuntu: Android devices in Docker (adbd_<UDID>) — services: RMDM, RDTSA, LRP, Reconciler (systemctl)
- AP=10.151.x.x, Dublin=10.100.x.x, US=10.146.x.x

Intents: create_jira | assign_ticket | send_invite | infra_issue | unknown

issue_categories: device_down|reboot|adb_issue|network_issue|db_mismatch|jenkins_failure|
app_crash|storage_issue|device_disconnected|lrr_down|resigner_down|ihm_down|reconciler_down|
lrp_down|rmdm_down|rdtsa_down|android_container_down|cert_expired|host_service_status

Return ONLY JSON:
{"intent":"...","confidence":0.0-1.0,"params":{"title":"","issue_type":"Task","assignee":"","cc":[],
"ticket_key":"","issue_category":"","devices":[],"region":null,"host_type":null}}

Rules:
- UDIDs: 40-char hex. IPs: 10.151→ap, 10.100→dublin, 10.146→us
- MISMATCH/device not found → device_disconnected
- Slack IDs: <@U...> format, 11 chars starting with U
- Use thread context for follow-up messages missing device info
"""

# Root cause: only called for 3+ correlated signals — keep prompt tight
ROOT_CAUSE_SYSTEM = """
Infra-Bot root cause analysis. Multiple DC issues in same channel.
Output concise Slack-formatted hypothesis (max 6 lines):
:mag: *Root Cause Analysis*
• *Likely cause:* <one line>
• *Evidence:* <signals>
• *Recommended action:* <next step>
Use :rotating_light: for network/rack-level incidents.
"""


class AIBrain:
    """Gemini primary, OpenAI fallback — used only when local classifier fails."""

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None
        self._openai_client = None
        self._cache: dict[str, tuple[float, any]] = {}  # key → (expires_ts, value)

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    @property
    def openai_client(self):
        if self._openai_client is None:
            if not settings.OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY is not set")
            from openai import OpenAI  # noqa: PLC0415
            self._openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)
        return self._openai_client

    def _cache_get(self, key: str):
        entry = self._cache.get(key)
        if entry and time.time() < entry[0]:
            logger.debug("Cache hit: %s", key[:20])
            return entry[1]
        return None

    def _cache_set(self, key: str, value, ttl: int) -> None:
        self._cache[key] = (time.time() + ttl, value)
        # Prune expired entries periodically
        if len(self._cache) > 200:
            now = time.time()
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}

    def _cache_key(self, prefix: str, text: str, extra: str = "") -> str:
        return hashlib.md5(f"{prefix}:{text}:{extra}".encode()).hexdigest()

    def _build_contents(
        self, text: str, thread_history: list[dict] | None = None
    ) -> list[types.Content]:
        contents: list[types.Content] = []
        # Only include last 3 thread messages to reduce token usage
        for msg in (thread_history or [])[-3:]:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append(
                types.Content(role=role, parts=[types.Part(text=msg["content"])])
            )
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        return contents

    def _call_with_retry(self, fn, retries: int = 2):
        """Call fn(), retrying once after 60s on 429 quota errors."""
        for attempt in range(retries):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                is_quota = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
                if is_quota and attempt < retries - 1:
                    logger.warning("Gemini 429 — waiting 60s before retry %d", attempt + 1)
                    time.sleep(60)
                    continue
                raise
        return None

    def _classify_openai(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Classify via OpenAI — called only when Gemini quota is exhausted."""
        messages = [{"role": "system", "content": CLASSIFY_SYSTEM}]
        for msg in (thread_history or [])[-3:]:
            role = "user" if msg.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": msg["content"]})
        messages.append({"role": "user", "content": text})

        resp = self.openai_client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=256,
        )
        return json.loads(resp.choices[0].message.content)

    def _rca_openai(self, signals_text: str) -> str:
        """Root cause analysis via OpenAI fallback."""
        resp = self.openai_client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": ROOT_CAUSE_SYSTEM},
                {"role": "user", "content": f"Signals:\n{signals_text}"},
            ],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()

    def classify(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Classify intent + extract params.

        Flow: local_classifier (caller) → Gemini → OpenAI fallback on 429.
        Results cached 5 min.
        """
        extra = str(len(thread_history)) if thread_history else ""
        cache_key = self._cache_key("classify", text[:200], extra)
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.debug("Classify cache hit")
            return cached

        gemini_quota_hit = False

        # ── Gemini ───────────────────────────────────────────────────────────
        try:
            contents = self._build_contents(text, thread_history)

            def _call():
                resp = self.client.models.generate_content(
                    model=MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=CLASSIFY_SYSTEM,
                        response_mime_type="application/json",
                        max_output_tokens=256,
                    ),
                )
                return json.loads(resp.text)

            result = self._call_with_retry(_call)
            if result:
                logger.info("Gemini classified: intent=%s confidence=%.2f",
                            result.get("intent"), result.get("confidence", 0))
                self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                return result
        except json.JSONDecodeError as exc:
            logger.error("Gemini returned invalid JSON: %s", exc)
        except Exception as exc:  # noqa: BLE001
            is_quota = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
            if is_quota:
                logger.warning("Gemini quota exhausted — trying OpenAI fallback")
                gemini_quota_hit = True
            else:
                logger.error("Gemini classify error: %s", exc)

        # ── OpenAI fallback ──────────────────────────────────────────────────
        if gemini_quota_hit and settings.OPENAI_API_KEY:
            try:
                result = self._classify_openai(text, thread_history)
                logger.info("OpenAI classified: intent=%s confidence=%.2f",
                            result.get("intent"), result.get("confidence", 0))
                self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                return result
            except json.JSONDecodeError as exc:
                logger.error("OpenAI returned invalid JSON: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("OpenAI classify error: %s", exc)
            return {"intent": "_quota_exceeded", "params": {}, "confidence": 0.0}

        if gemini_quota_hit:
            return {"intent": "_quota_exceeded", "params": {}, "confidence": 0.0}

        return {"intent": "unknown", "params": {}, "confidence": 0.0}

    def analyze_root_cause(self, signals_text: str) -> str:
        """Produce root cause diagnosis from correlated signals.

        Tries Gemini first, falls back to OpenAI on quota exhaustion.
        Cached 10 min.
        """
        cache_key = self._cache_key("rca", signals_text)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        gemini_quota_hit = False
        try:
            def _call():
                resp = self.client.models.generate_content(
                    model=MODEL,
                    contents=f"Signals:\n{signals_text}",
                    config=types.GenerateContentConfig(
                        system_instruction=ROOT_CAUSE_SYSTEM,
                        max_output_tokens=200,
                    ),
                )
                return resp.text.strip()

            result = self._call_with_retry(_call)
            if result:
                self._cache_set(cache_key, result, _ROOT_CAUSE_CACHE_TTL)
                return result
        except Exception as exc:  # noqa: BLE001
            is_quota = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
            if is_quota:
                gemini_quota_hit = True
            else:
                logger.error("analyze_root_cause error: %s", exc)

        if gemini_quota_hit and settings.OPENAI_API_KEY:
            try:
                result = self._rca_openai(signals_text)
                self._cache_set(cache_key, result, _ROOT_CAUSE_CACHE_TTL)
                return result
            except Exception as exc:  # noqa: BLE001
                logger.error("OpenAI RCA error: %s", exc)

        return ":mag: *Root Cause Analysis*\n• Multiple correlated signals — manual investigation recommended"


# ---------------------------------------------------------------------------
# Template responses — replaces generate_response() for known action outcomes
# No Gemini call needed for these common cases.
# ---------------------------------------------------------------------------

def _jira_created_reply(result: dict) -> str:
    if not result.get("success"):
        err = result.get("error", "unknown error")
        return f":x: Failed to create Jira ticket — {err}"
    key  = result.get("key", "?")
    url  = result.get("url", "")
    title = result.get("title", "")
    assignee = result.get("assignee_id", "")
    link = f"<{url}|{key}>" if url else key
    reply = f"Done :white_check_mark: Created {link}"
    if title:
        reply += f" — _{title}_"
    if assignee:
        reply += f"\nAssigned to <@{assignee}>"
    return reply


def _jira_assigned_reply(result: dict) -> str:
    if not result.get("success"):
        err = result.get("error", "unknown error")
        return f":x: Failed to assign ticket — {err}"
    key      = result.get("key", "?")
    assignee = result.get("assignee_id", "")
    reply = f"Done :white_check_mark: {key} assigned"
    if assignee:
        reply += f" to <@{assignee}>"
    return reply


def _unclear_reply(text: str) -> str:
    return (
        ":thinking_face: Not sure what you mean. Try:\n"
        "• `@infra-bot device 10.151.x.x is down`\n"
        "• `@infra-bot LRR down on 10.151.x.x`\n"
        "• `@infra-bot create jira: <title>`\n"
        "• `@infra-bot what can you do`"
    )


def _invite_reply(params: dict) -> str:
    return (
        ":calendar: Got it — calendar invite feature coming soon. "
        "For now, please create a Google Calendar event manually."
    )


# Module-level singleton
brain = AIBrain()
