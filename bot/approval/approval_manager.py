"""Approval manager: persistent action store backed by Redis.

Keys:
  infra:approval:{action_id}  -> JSON-serialised ActionRecord (TTL: 30 min)
  infra:approvals:index       -> Redis set of known action IDs (for /infra pending)
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
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


def _serialize(record: ActionRecord) -> str:
    return json.dumps(asdict(record))


def _deserialize(raw: str) -> ActionRecord:
    return ActionRecord(**json.loads(raw))


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
    ) -> str:
        action_id = str(uuid.uuid4())[:8]
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
        r = get_redis()
        r.setex(f"{_PREFIX}{action_id}", ACTION_TTL_SECONDS, _serialize(record))
        r.sadd(_INDEX_KEY, action_id)
        r.expire(_INDEX_KEY, ACTION_TTL_SECONDS * 2)
        logger.info("Action created in Redis: %s (%s)", action_id, action_type)
        return action_id

    def _load(self, action_id: str) -> Optional[ActionRecord]:
        raw = get_redis().get(f"{_PREFIX}{action_id}")
        return _deserialize(raw) if raw else None

    def _save(self, record: ActionRecord) -> None:
        r = get_redis()
        key = f"{_PREFIX}{record.action_id}"
        ttl = r.ttl(key)
        r.setex(key, max(ttl, 60), _serialize(record))  # keep at least 60s

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

    def deny(self, action_id: str, denier_id: str) -> Optional[ActionRecord]:
        record = self._load(action_id)
        if not record or record.status != "pending":
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
        """Return all currently pending actions (used by /infra pending)."""
        r = get_redis()
        action_ids = r.smembers(_INDEX_KEY)
        pending = []
        for aid in action_ids:
            record = self._load(aid)
            if record and record.status == "pending":
                pending.append(record)
        return sorted(pending, key=lambda x: x.requested_at)

    def cleanup_expired(self) -> None:
        """Prune ghost IDs from the index (keys that have TTL-expired)."""
        r = get_redis()
        for aid in r.smembers(_INDEX_KEY):
            if not r.exists(f"{_PREFIX}{aid}"):
                r.srem(_INDEX_KEY, aid)


approval_manager = ApprovalManager()
