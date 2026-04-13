"""Approval manager: persistent action store backed by Redis.

Keys:
  infra:approval:{action_id}        -> JSON-serialised ActionRecord (TTL: 30 min)
  infra:approvals:index             -> Redis set of known action IDs
  infra:approval:msgts:{ch}:{ts}    -> action_id (for reaction-based approval)
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from bot.memory.redis_client import get_redis
from utils.logger import get_logger

logger = get_logger(__name__)

ACTION_TTL_SECONDS: int = 30 * 60
_PREFIX = "infra:approval:"
_INDEX_KEY = "infra:approvals:index"


@dataclass
class ActionRecord:
    action_id: str
    action_type: str
    params: dict
    channel: str
    thread_ts: str
    requested_by: str
    requested_at: float = field(default_factory=time.time)
    status: str = "pending"   # pending|approved|denied|expired|completed|failed
    region: str = "unknown"
    devices: list = field(default_factory=list)
    result: Optional[dict] = None
    dry_run_preview: Optional[str] = None
    approval_msg_ts: Optional[str] = None   # ts of the Block Kit card (for editing)


def _serialize(record: ActionRecord) -> str:
    return json.dumps(asdict(record))


def _deserialize(raw: str) -> ActionRecord:
    return ActionRecord(**json.loads(raw))


_MAX_WATCHER_THREADS = 50  # prevent unbounded accumulation under high load


_DEDUP_TTL_SECONDS = 120  # 2-min window to block duplicate action creation

class ApprovalManager:
    def create_action(
        self,
        action_type: str,
        params: dict,
        channel: str,
        thread_ts: str,
        requested_by: str,
        region: str = "unknown",
        devices: Optional[list] = None,
        dry_run_preview: Optional[str] = None,
        trace_id: str = "",
    ) -> str:
        # Fix #10: dedup guard — block same action_type+host+udid within 2 minutes
        # Prevents double-execution (e.g. LRR restart firing twice for the same device)
        host = params.get("host", "")
        udid = params.get("udid", "")
        dedup_key = f"infra:action:dedup:{action_type}:{host}:{udid}"
        r = get_redis()
        existing_id = r.get(dedup_key)
        if existing_id:
            existing_id = existing_id.decode() if isinstance(existing_id, bytes) else existing_id
            logger.warning(
                "[%s] Duplicate action blocked: %s host=%s udid=%s — existing action_id=%s",
                trace_id, action_type, host, udid, existing_id,
            )
            return existing_id

        action_id = str(uuid.uuid4())[:8]
        r.setex(dedup_key, _DEDUP_TTL_SECONDS, action_id)

        record = ActionRecord(
            action_id=action_id,
            action_type=action_type,
            params=params,
            channel=channel,
            thread_ts=thread_ts,
            requested_by=requested_by,
            region=region,
            devices=devices or [],
            dry_run_preview=dry_run_preview,
        )
        r.setex(f"{_PREFIX}{action_id}", ACTION_TTL_SECONDS, _serialize(record))
        r.sadd(_INDEX_KEY, action_id)
        r.expire(_INDEX_KEY, ACTION_TTL_SECONDS * 2)
        logger.info("[%s] Action created in Redis: %s (%s) host=%s udid=%s",
                    trace_id, action_id, action_type, host, udid)
        return action_id

    def set_msg_ts(self, action_id: str, msg_ts: str, channel: str) -> None:
        """Store the Slack message ts of the approval card for later editing."""
        record = self._load(action_id)
        if record:
            record.approval_msg_ts = msg_ts
            self._save(record)
        # Also store reverse lookup for reaction-based approval
        get_redis().setex(
            f"infra:approval:msgts:{channel}:{msg_ts}",
            ACTION_TTL_SECONDS,
            action_id,
        )

    def _load(self, action_id: str) -> Optional[ActionRecord]:
        raw = get_redis().get(f"{_PREFIX}{action_id}")
        return _deserialize(raw) if raw else None

    def _save(self, record: ActionRecord) -> None:
        r = get_redis()
        key = f"{_PREFIX}{record.action_id}"
        ttl = r.ttl(key)
        # ttl > 0: key has expiry — preserve it; ttl <= 0: expired/missing/no-expiry — use default
        effective_ttl = ttl if ttl > 0 else ACTION_TTL_SECONDS
        r.setex(key, effective_ttl, _serialize(record))

    def get_action(self, action_id: str) -> Optional[ActionRecord]:
        return self._load(action_id)

    def approve(self, action_id: str, approver_id: str) -> Optional[ActionRecord]:
        record = self._load(action_id)
        if not record or record.status != "pending":
            return None
        record.status = "approved"
        self._save(record)
        logger.info("Action %s approved by %s", action_id, approver_id)
        return record

    def pre_approve(self, action_id: str, approver_id: str) -> Optional[ActionRecord]:
        """First-step approval for double-approval actions (pending → pre_approved)."""
        record = self._load(action_id)
        if not record or record.status != "pending":
            return None
        record.status = "pre_approved"
        self._save(record)
        logger.info("Action %s pre-approved by %s (awaiting second confirmation)", action_id, approver_id)
        return record

    def confirm_approve(self, action_id: str, approver_id: str) -> Optional[ActionRecord]:
        """Second-step approval for double-approval actions (pre_approved → approved)."""
        record = self._load(action_id)
        if not record or record.status != "pre_approved":
            return None
        record.status = "approved"
        self._save(record)
        logger.info("Action %s confirmed (second approval) by %s", action_id, approver_id)
        return record

    def deny(self, action_id: str, denier_id: str) -> Optional[ActionRecord]:
        record = self._load(action_id)
        if not record or record.status not in ("pending", "pre_approved"):
            return None
        record.status = "denied"
        self._save(record)
        logger.info("Action %s denied by %s", action_id, denier_id)
        return record

    def complete(self, action_id: str, result: dict) -> None:
        record = self._load(action_id)
        if record:
            record.status = "completed" if result.get("success") else "failed"
            record.result = result
            self._save(record)

    def list_pending(self) -> list[ActionRecord]:
        r = get_redis()
        action_ids = r.smembers(_INDEX_KEY)
        pending = []
        for aid in action_ids:
            record = self._load(aid)
            if record and record.status == "pending":
                pending.append(record)
        return sorted(pending, key=lambda x: x.requested_at)

    def cleanup_expired(self) -> None:
        r = get_redis()
        for aid in r.smembers(_INDEX_KEY):
            if not r.exists(f"{_PREFIX}{aid}"):
                r.srem(_INDEX_KEY, aid)

    def start_escalation_watcher(
        self,
        action_id: str,
        channel: str,
        thread_ts: str,
        client,  # Slack WebClient
    ) -> None:
        """Start a daemon thread that escalates if approval doesn't arrive in time."""
        from config import settings  # noqa: PLC0415

        if not settings.ESCALATION_WAIT_MINUTES:
            return

        # Don't let watcher threads pile up under high load
        active = sum(1 for t in threading.enumerate() if t.name.startswith("escalation_watcher:"))
        if active >= _MAX_WATCHER_THREADS:
            logger.warning("Escalation watcher limit reached (%d) — skipping watcher for %s", active, action_id)
            return

        def _watcher() -> None:
            wait = settings.ESCALATION_WAIT_MINUTES * 60
            time.sleep(wait)

            record = self.get_action(action_id)
            if not record or record.status != "pending":
                return

            # First escalation: DM the approver
            try:
                client.chat_postMessage(
                    channel=settings.APPROVER_SLACK_ID,
                    text=(
                        f":timer_clock: Action `{action_id}` (`{record.action_type}`) "
                        f"has been pending for {settings.ESCALATION_WAIT_MINUTES} min — "
                        f"please approve or deny in <#{channel}>"
                    ),
                )
                logger.info("Escalation DM sent for action %s", action_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Escalation DM failed: %s", exc)

            # Second escalation: mention backup approver in thread
            if settings.ESCALATION_APPROVER_ID:
                time.sleep(wait)
                record = self.get_action(action_id)
                if not record or record.status != "pending":
                    return
                try:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f":loudspeaker: <@{settings.ESCALATION_APPROVER_ID}> escalating — "
                            f"action `{action_id}` (`{record.action_type}`) "
                            f"still needs approval after {settings.ESCALATION_WAIT_MINUTES * 2} min"
                        ),
                    )
                    logger.info("Escalation to backup approver for action %s", action_id)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Backup escalation failed: %s", exc)

        threading.Thread(target=_watcher, daemon=True, name=f"escalation_watcher:{action_id}").start()


approval_manager = ApprovalManager()
