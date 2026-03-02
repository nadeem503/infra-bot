"""JSON audit logger for action tracking.

Logs to logs/actions.jsonl in JSON Lines format.
Never logs raw credentials.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("logs")
AUDIT_LOG_FILE = LOG_DIR / "actions.jsonl"


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def ensure_log_dir() -> None:
    LOG_DIR.mkdir(exist_ok=True)


def hash_params(params: dict) -> str:
    """Return a short SHA-256 hex of sanitised params (credentials excluded)."""
    sensitive = {"password", "token", "key", "secret", "credential", "api_token"}
    safe = {k: v for k, v in params.items() if not any(s in k.lower() for s in sensitive)}
    return hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()[:16]


def audit_log(
    action_type: str,
    triggered_by: str,
    channel: str,
    devices: list,
    region: str,
    params: dict,
    status: str,
    result_summary: str = "",
) -> None:
    """Append a structured JSON Lines audit entry to logs/actions.jsonl."""
    ensure_log_dir()
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action_type": action_type,
        "triggered_by": triggered_by,
        "channel": channel,
        "devices": devices,
        "region": region,
        "params_hash": hash_params(params),
        "status": status,
        "result_summary": result_summary,
    }
    with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    get_logger("audit").info(
        "AUDIT: %s | %s | %s | triggered_by=%s",
        action_type, status, region, triggered_by,
    )
