"""AI brain for infra-bot.

Flow: local_classifier → Claude CLI → Gemini fallback.
Claude CLI uses the ltadmin Claude Code subscription (zero API cost).
Claude acts as smart router: classifies OR replies directly for complex questions.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from typing import Optional

from google import genai
from google.genai import types

from config import settings
from utils.activity_log import log_claude_call, log_bot_session
from utils.logger import get_logger

logger = get_logger(__name__)

_QUOTA_MSG = (
    ":hourglass: AI is unavailable right now. Try again in a moment."
)

# Claude CLI path on the bot host
_CLAUDE_BIN = "/opt/homebrew/bin/claude"
_KEYCHAIN_DB = "/Users/ltadmin/Library/Keychains/login.keychain-db"

_keychain_unlocked = False  # unlocked once per process lifetime


def _ensure_keychain_unlocked() -> None:
    """Unlock the login keychain so Claude CLI can read its OAuth token.

    Idempotent — runs at most once per process. Required when bot starts
    as a background process (nohup/SSH) where keychain is locked.
    """
    global _keychain_unlocked
    if _keychain_unlocked:
        return
    try:
        passwd = settings.HOST_PASS or "lambdatest123!"
        result = subprocess.run(
            ["security", "unlock-keychain", "-p", passwd, _KEYCHAIN_DB],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Keychain unlocked for Claude CLI")
            _keychain_unlocked = True
        else:
            logger.warning("Keychain unlock failed: %s", result.stderr.strip()[:100])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Keychain unlock error: %s", exc)

MODEL = "gemini-2.0-flash"

# Cache TTL: 5 min for classify, 10 min for root cause
_CLASSIFY_CACHE_TTL = 300
_ROOT_CAUSE_CACHE_TTL = 600

# ---------------------------------------------------------------------------
# Claude router prompt — Claude decides: classify OR reply directly.
# ---------------------------------------------------------------------------

CLAUDE_ROUTER_SYSTEM = """
You are Infra-Bot, a Slack assistant for LambdaTest's Real Device Cloud infrastructure team.

Read the Slack message and choose ONE of three actions:

ACTION 1 — route_local: The message is a clear, unambiguous infra/ops command that matches
a simple pattern (IP down, service restart, device check, Jira create/assign, ADB restart, etc).
The local rule engine can handle it reliably without your intelligence.
Use this for straightforward operational commands where intent is obvious.

ACTION 2 — classify: The message needs your reasoning to extract intent + params accurately.
Use this when the message is infra-related but ambiguous, has complex context, mentions
multiple devices/services, needs thread context to interpret, or local rules would mis-classify it.

ACTION 3 — direct: The message needs an intelligent conversational reply — explanations,
troubleshooting advice, summaries, questions, anything that isn't a structured bot action.
Use this for: "what happened?", "why is X failing?", "summarize this", "explain Y", etc.

Host context:
- macOS hosts: iOS devices — services: LRR, Resigner (port 6789), IHM, LRP, Reconciler (launchctl)
- Ubuntu hosts: Android devices in Docker (adbd_<UDID>) — services: RMDM, RDTSA, LRP, Reconciler (systemctl)
- AP=10.151.x.x  Dublin=10.100.x.x  US=10.146.x.x
- UDIDs: iOS old=40 hex chars, iOS new=XXXXXXXX-XXXXXXXXXXXXXXXX (8hex-dash-16hex). Android serials: alphanumeric 6-20 chars.

For ACTION 1 (route_local): {"action":"route_local"}

For ACTION 2 (classify):
{"action":"classify","intent":"<intent>","confidence":0.0-1.0,"params":{"title":"","issue_type":"Task","assignee":"","cc":[],"ticket_key":"","issue_category":"","devices":[],"region":null,"host_type":null}}

Valid intents: create_jira | assign_ticket | send_invite | infra_issue | device_check | unknown
Valid issue_categories: device_down | reboot | adb_issue | network_issue | db_mismatch |
jenkins_failure | app_crash | storage_issue | device_disconnected | lrr_down | resigner_down |
ihm_down | reconciler_down | lrp_down | rmdm_down | rdtsa_down | android_container_down |
cert_expired | host_service_status

For ACTION 3 (direct):
{"action":"direct","reply":"<slack-formatted response, *bold*, bullet points, max 8 lines>"}

Rules:
- Slack user IDs: <@U04UTG30V9A> → extract U... part (9-12 chars starting with U)
- IP prefix → region: 10.151→ap, 10.100→dublin, 10.146→us
- MISMATCH / device not found → intent=infra_issue, issue_category=device_disconnected
- Reply ONLY with valid JSON. No markdown fences, no explanation outside the JSON.
"""

# Gemini-only classification prompt (used as fallback when Claude CLI fails)
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
- UDIDs: iOS old=40 hex chars, iOS new=XXXXXXXX-XXXXXXXXXXXXXXXX (8hex-dash-16hex). IPs: 10.151→ap, 10.100→dublin, 10.146→us
- MISMATCH/device not found → device_disconnected
- Slack IDs: <@U...> format, 11 chars starting with U
- Use thread context for follow-up messages missing device info
"""

# Root cause: only called for 3+ correlated signals
ROOT_CAUSE_SYSTEM = """
Infra-Bot root cause analysis. Multiple DC issues in same channel.
Output concise Slack-formatted hypothesis (max 6 lines):
:mag: *Root Cause Analysis*
• *Likely cause:* <one line>
• *Evidence:* <signals>
• *Recommended action:* <next step>
Use :rotating_light: for network/rack-level incidents.
"""


def _call_claude_cli(prompt: str, timeout: int = 30, _log_action: str = "") -> str:
    """Run claude -p <prompt> as subprocess. Returns stdout text or raises."""
    import os
    _ensure_keychain_unlocked()
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/Users/ltadmin"),
        "USER": os.environ.get("USER", "ltadmin"),
        "LOGNAME": os.environ.get("LOGNAME", "ltadmin"),
    }
    t0 = time.time()
    try:
        result = subprocess.run(
            [_CLAUDE_BIN, "-p", prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        duration = int((time.time() - t0) * 1000)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            log_claude_call(prompt[:120], "", duration, False,
                            action=_log_action, error=err[:200])
            raise RuntimeError(f"claude CLI exited {result.returncode}: {err[:200]}")
        output = result.stdout.strip()
        log_claude_call(prompt[:120], output[:200], duration, True, action=_log_action)
        return output
    except subprocess.TimeoutExpired:
        duration = int((time.time() - t0) * 1000)
        log_claude_call(prompt[:120], "", duration, False,
                        action=_log_action, error="timeout")
        raise


class AIBrain:
    """Claude CLI primary (smart router) → Gemini fallback — used when local classifier fails."""

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None
        self._cache: dict[str, tuple[float, any]] = {}  # key → (expires_ts, value)

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    def _cache_get(self, key: str):
        entry = self._cache.get(key)
        if entry and time.time() < entry[0]:
            logger.debug("Cache hit: %s", key[:20])
            return entry[1]
        return None

    def _cache_set(self, key: str, value, ttl: int) -> None:
        self._cache[key] = (time.time() + ttl, value)
        if len(self._cache) > 200:
            now = time.time()
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}

    def _cache_key(self, prefix: str, text: str, extra: str = "") -> str:
        return hashlib.md5(f"{prefix}:{text}:{extra}".encode()).hexdigest()

    def _build_contents(
        self, text: str, thread_history: list[dict] | None = None
    ) -> list[types.Content]:
        contents: list[types.Content] = []
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

    def classify(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Classify intent + extract params, or get a direct reply from Claude.

        Flow: Claude CLI (smart router) → Gemini fallback.
        Claude CLI can either classify (return structured params) or reply directly.
        Results cached 5 min.
        """
        extra = str(len(thread_history)) if thread_history else ""
        cache_key = self._cache_key("classify", text[:200], extra)
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.debug("Classify cache hit")
            return cached

        # ── Claude CLI (primary — free, uses ltadmin subscription) ───────────
        try:
            thread_ctx = ""
            for msg in (thread_history or [])[-3:]:
                role = "User" if msg.get("role") == "user" else "Bot"
                thread_ctx += f"{role}: {msg['content']}\n"

            prompt = (
                f"{CLAUDE_ROUTER_SYSTEM}\n\n"
                f"{('Thread context:\n' + thread_ctx) if thread_ctx else ''}"
                f"Message: {text}\n\n"
                f"Reply with ONLY valid JSON."
            )
            raw = _call_claude_cli(prompt, timeout=30, _log_action="router")

            # Extract JSON from response
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                action = parsed.get("action", "classify")

                if action == "route_local":
                    # Claude says: simple pattern — let local classifier handle it
                    result = {"intent": "_route_local", "confidence": 1.0,
                              "params": {}, "_source": "claude"}
                    logger.info("Claude CLI → route_local")
                    # Don't cache route_local — local classifier decides per-message
                    return result

                elif action == "direct":
                    # Claude is handling this directly — wrap as _direct_reply intent
                    reply = parsed.get("reply", "")
                    if reply:
                        result = {
                            "intent": "_direct_reply",
                            "confidence": 1.0,
                            "params": {"reply": reply},
                            "_source": "claude",
                        }
                        log_claude_call(prompt[:120], reply[:200], 0, True,
                                        action="direct", intent="_direct_reply")
                        logger.info("Claude CLI direct reply (len=%d)", len(reply))
                        self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                        return result

                elif action == "classify":
                    # Claude classified — return as standard classification dict
                    intent_val = parsed.get("intent", "unknown")
                    result = {
                        "intent": intent_val,
                        "confidence": parsed.get("confidence", 0.7),
                        "params": parsed.get("params", {}),
                        "_source": "claude",
                    }
                    log_claude_call(prompt[:120], raw[:200], 0, True,
                                    action="classify", intent=intent_val)
                    logger.info("Claude CLI classified: intent=%s confidence=%.2f",
                                result["intent"], result["confidence"])
                    self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                    return result

        except subprocess.TimeoutExpired:
            logger.warning("Claude CLI timed out — falling back to Gemini")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Claude CLI classify failed: %s — falling back to Gemini", exc)

        # ── Gemini fallback (Claude failed entirely) ─────────────────────────
        return self.classify_gemini(text, thread_history)

    def classify_gemini(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Gemini-only classification — called as last-resort fallback."""
        extra = str(len(thread_history)) if thread_history else ""
        cache_key = self._cache_key("classify_gemini", text[:200], extra)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

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
                result["_source"] = "gemini"
                logger.info("Gemini classified: intent=%s confidence=%.2f",
                            result.get("intent"), result.get("confidence", 0))
                self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                return result
        except json.JSONDecodeError as exc:
            logger.error("Gemini returned invalid JSON: %s", exc)
        except Exception as exc:  # noqa: BLE001
            is_quota = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
            if is_quota:
                logger.warning("Gemini quota exhausted")
                return {"intent": "_quota_exceeded", "params": {}, "confidence": 0.0,
                        "_source": "gemini"}
            logger.error("Gemini classify error: %s", exc)

        return {"intent": "unknown", "params": {}, "confidence": 0.0, "_source": "gemini"}

    def analyze_root_cause(self, signals_text: str) -> str:
        """Produce root cause diagnosis from correlated signals.

        Tries Claude CLI first, falls back to Gemini.
        Cached 10 min.
        """
        cache_key = self._cache_key("rca", signals_text)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        # ── Claude CLI ────────────────────────────────────────────────────────
        try:
            prompt = f"{ROOT_CAUSE_SYSTEM}\n\nSignals:\n{signals_text}"
            result = _call_claude_cli(prompt, timeout=30, _log_action="rca")
            if result:
                self._cache_set(cache_key, result, _ROOT_CAUSE_CACHE_TTL)
                return result
        except subprocess.TimeoutExpired:
            logger.warning("Claude CLI RCA timed out — falling back to Gemini")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Claude CLI RCA failed: %s — falling back to Gemini", exc)

        # ── Gemini fallback ──────────────────────────────────────────────────
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
            logger.error("analyze_root_cause error: %s", exc)

        return ":mag: *Root Cause Analysis*\n• Multiple correlated signals — manual investigation recommended"


# ---------------------------------------------------------------------------
# Template responses — replaces generate_response() for known action outcomes
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
