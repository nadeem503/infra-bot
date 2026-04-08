"""Local rule-based classifier — runs BEFORE Gemini to avoid API calls.

Handles ~80% of messages without spending a single Gemini token.
Only passes to Gemini when local rules can't produce high-confidence result.

Decision flow:
  1. Jira patterns (create_jira / assign_ticket) — deterministic regex
  2. infra_issue keyword match from keywords.yaml
  3. IP/UDID/hostname extraction
  4. Region detection from IP
  → if confident: return result (skip Gemini)
  → if ambiguous or thread follow-up with no device: return None (call Gemini)
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from utils.config_loader import get_keywords
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

_IP_RE      = re.compile(r'\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b')
_UDID_RE    = re.compile(r'\b([0-9a-fA-F]{40})\b')                 # iOS UDID (40-char hex)
_ANDROID_SERIAL_RE = re.compile(r'\b([A-Z][A-Z0-9]{7,19})\b')     # Android serial: uppercase, 8-20 chars
_JIRA_KEY   = re.compile(r'\bTE-\d+\b', re.IGNORECASE)

# Detects connectivity-check queries: "check connected", "is it online", etc.
_DEVICE_CHECK_RE = re.compile(
    r'\b(check|verify|ping|is|test)\b.{0,40}\b(connected|online|alive|reachable|up|running)\b'
    r'|\b(connected|online|reachable)\b.{0,20}\b(on host|to host|on device)\b'
    r'|\b(check|verify)\b.{0,20}\b(connected|connection|connectivity|status|if connected)\b',
    re.IGNORECASE,
)

# Matches lines in the format "10.x.x.x,SERIAL" — device mapping list
_DEVICE_MAPPING_LINE_RE = re.compile(
    r'^\s*10\.\d{1,3}\.\d{1,3}\.\d{1,3}\s*,\s*[A-Z][A-Z0-9]{6,}\s*$',
    re.MULTILINE,
)

_CREATE_JIRA_RE = re.compile(
    r'\b(create|open|add|file|raise|log)\b.{0,30}\b(jira|ticket|task|bug|story|issue)\b',
    re.IGNORECASE,
)
_ASSIGN_RE = re.compile(
    r'\bassign\b.{0,20}\bTE-\d+\b|\bTE-\d+\b.{0,20}\bassign\b',
    re.IGNORECASE,
)

# IP prefix → region
_IP_REGIONS = {
    "10.151": "ap",
    "10.100": "dublin",
    "10.146": "us",
}

# Minimum keyword hits to trust local classification
_MIN_KEYWORD_HITS = 1

# These issue categories are too ambiguous for local-only classification
# (e.g. "down" matches too broadly) — always verify with Gemini
_AMBIGUOUS_CATEGORIES = {"db_mismatch"}


@lru_cache(maxsize=1)
def _load_keywords() -> dict:
    return get_keywords()


def _extract_devices(text: str) -> list[str]:
    devices: list[str] = []
    devices.extend(_UDID_RE.findall(text))
    # Android serials: uppercase alphanumeric, must contain at least one digit
    for m in _ANDROID_SERIAL_RE.findall(text):
        if any(c.isdigit() for c in m):  # filter out all-letter words like "MISMATCH"
            devices.append(m)
    devices.extend(_IP_RE.findall(text))
    return list(dict.fromkeys(devices))  # deduplicate, preserve order


def _detect_region(devices: list[str], text: str) -> Optional[str]:
    region_counts: dict[str, int] = {}
    for device in devices:
        for prefix, region in _IP_REGIONS.items():
            if device.startswith(prefix):
                region_counts[region] = region_counts.get(region, 0) + 1
    if region_counts:
        # Return most common region; if tied, prefer "ap" then "us" then "dublin"
        return max(region_counts, key=lambda r: (region_counts[r], r == "ap", r == "us"))
    # Fallback: text keywords
    t = text.lower()
    if any(k in t for k in ("mumbai", "mum", "mum-dc", " ap ", "apac", "singapore")):
        return "ap"
    if any(k in t for k in ("dublin", "ireland", " eu ", "euw")):
        return "dublin"
    if any(k in t for k in (" us ", "usa", "california", "virginia")):
        return "us"
    if any(k in t for k in ("india", "blr", "bangalore", "hyderabad")):
        return "india"
    return None


def _detect_host_type(text: str) -> Optional[str]:
    t = text.lower()
    macos_signals = ("launchctl", "idevice_id", "plist", "xcode", "wda", "ios ", "resigner",
                     "lrr", "ihm", "lambda_remote_runner", "macos", "mac host")
    ubuntu_signals = ("systemctl", "docker ", "adbd_", "rmdm", "rdtsa", "ubuntu", "android ")
    if any(s in t for s in macos_signals):
        return "macos"
    if any(s in t for s in ubuntu_signals):
        return "ubuntu"
    return None


def _match_issue_category(text: str) -> Optional[tuple[str, int]]:
    """Return (best_category, hit_count) or None if no match."""
    keywords_cfg = _load_keywords()
    t = text.lower()
    best_cat: Optional[str] = None
    best_hits = 0

    for category, cfg in keywords_cfg.items():
        kws = cfg.get("keywords", [])
        hits = sum(1 for kw in kws if kw.lower() in t)
        if hits > best_hits:
            best_hits = hits
            best_cat = category

    if best_cat and best_hits >= _MIN_KEYWORD_HITS:
        return best_cat, best_hits
    return None


def classify_local(text: str, thread_history: list[dict] | None = None) -> Optional[dict]:
    """Try to classify without Gemini.

    Returns a classification dict (same shape as brain.classify) if confident,
    or None to signal "needs Gemini".
    """
    # Strip bot mention tag
    clean = re.sub(r'<@[A-Z0-9]+>', '', text).strip()

    # ── 1. Jira assign: "assign TE-123 to @user" ─────────────────────────────
    if _ASSIGN_RE.search(clean):
        keys = _JIRA_KEY.findall(clean)
        # Extract assignee Slack ID
        assignee_match = re.search(r'<@([A-Z0-9]{9,12})>', text)
        return {
            "intent": "assign_ticket",
            "confidence": 0.95,
            "params": {
                "ticket_key": keys[0].upper() if keys else "",
                "assignee": assignee_match.group(1) if assignee_match else "",
                "title": "", "issue_type": "Task", "cc": [],
                "devices": [], "region": None, "host_type": None,
            },
            "_source": "local",
        }

    # ── 2. Create Jira ticket ─────────────────────────────────────────────────
    if _CREATE_JIRA_RE.search(clean):
        # Extract title: everything after the trigger phrase
        title_match = re.search(
            r'(?:create|open|add|file|raise|log)\s+(?:a\s+)?(?:jira\s+)?'
            r'(?:ticket|task|bug|story|issue)(?:\s*[:–-]\s*|\s+for\s+|\s+)(.*)',
            clean, re.IGNORECASE,
        )
        title = title_match.group(1).strip() if title_match else clean
        # Detect issue type
        issue_type = "Bug" if re.search(r'\bbug\b', clean, re.IGNORECASE) else \
                     "Story" if re.search(r'\bstory\b', clean, re.IGNORECASE) else "Task"
        assignee_match = re.search(r'<@([A-Z0-9]{9,12})>', text)
        return {
            "intent": "create_jira",
            "confidence": 0.93,
            "params": {
                "title": title,
                "issue_type": issue_type,
                "assignee": assignee_match.group(1) if assignee_match else "",
                "cc": [], "ticket_key": "",
                "devices": [], "region": None, "host_type": None,
            },
            "_source": "local",
        }

    # ── 3. Device mapping list ("10.x.x.x,SERIAL" per line) → device_check ──────
    # When the message is a host,device mapping list, always treat it as a
    # connectivity check — never as device_down (which requires approval).
    mapping_lines = _DEVICE_MAPPING_LINE_RE.findall(clean)
    if len(mapping_lines) >= 2:
        devices = _extract_devices(clean)
        hosts   = [d for d in devices if d.startswith("10.")]
        udids   = [d for d in devices if not d.startswith("10.")]
        region  = _detect_region(hosts, clean)
        return {
            "intent": "device_check",
            "confidence": 0.88,
            "params": {
                "host":  hosts[0] if hosts else "",
                "udid":  udids[0] if udids else "",
                "hosts": hosts,
                "udids": udids,
                "devices": devices,
                "region": region,
                "host_type": None,
                "title": "", "issue_type": "Task", "assignee": "",
                "cc": [], "ticket_key": "",
            },
            "_source": "local",
        }

    # ── 4. Device connectivity check ("check if connected", "is it online") ─────
    # This is a READ-ONLY check — runs adb devices on the host directly,
    # no Gemini, no approval workflow.
    if _DEVICE_CHECK_RE.search(clean):
        devices = _extract_devices(clean)
        # Split: IP addresses → host, everything else (UDID/serial) → device
        host = next((d for d in devices if d.startswith("10.")), "")
        udid = next((d for d in devices if not d.startswith("10.")), "")
        region = _detect_region(devices, clean)
        return {
            "intent": "device_check",
            "confidence": 0.90,
            "params": {
                "host": host,
                "udid": udid,
                "devices": devices,
                "region": region,
                "host_type": None,
                "title": "", "issue_type": "Task", "assignee": "",
                "cc": [], "ticket_key": "",
            },
            "_source": "local",
        }

    # ── 5. Infra issue via keyword match ──────────────────────────────────────
    match = _match_issue_category(clean)
    if not match:
        # No keyword match and no thread context → let Gemini handle
        return None

    category, hits = match

    # Ambiguous categories always go to Gemini for disambiguation
    if category in _AMBIGUOUS_CATEGORIES:
        return None

    devices = _extract_devices(clean)
    region  = _detect_region(devices, clean)
    host_type = _detect_host_type(clean)

    # For device_disconnected: must have a device identifier
    if category == "device_disconnected" and not devices:
        return None

    # Confidence: scale with keyword hits, penalise if no device found
    confidence = min(0.60 + (hits * 0.10), 0.92)
    if not devices:
        confidence -= 0.10  # lower if no device extracted
    if confidence < 0.55:
        return None

    return {
        "intent": "infra_issue",
        "confidence": round(confidence, 2),
        "params": {
            "issue_category": category,
            "devices": devices,
            "region": region,
            "host_type": host_type,
            "title": "", "issue_type": "Task", "assignee": "",
            "cc": [], "ticket_key": "",
        },
        "_source": "local",
    }
