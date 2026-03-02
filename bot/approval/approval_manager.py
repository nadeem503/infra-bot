"""Approval manager: stores pending actions with 30-minute TTL.

Currently in-memory — replace _store with a Redis client to scale.
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

ACTION_TTL_SECONDS: int = 30 * 60


@dataclass
class ActionRecord:
    action_id: str
    action_type: str
    params: dict
    channel: str
    thread_ts: str
    requested_by: str
    requested_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending|approved|denied|expired|completed|failed
    region: str = "unknown"
    devices: list = field(default_factory=list)
    result: Optional[dict] = None


class ApprovalManager:
    def __init__(self) -> None:
        self._store: dict[str, ActionRecord] = {}

    def create_action(
        self,
        action_type: str,
        params: dict,
        channel: str,
        thread_ts: str,
        requested_by: str,
        region: str = "unknown",
        devices: Optional[list] = None,
    ) -> str:
        action_id = str(uuid.uuid4())[:8]
        self._store[action_id] = ActionRecord(
            action_id=action_id,
            action_type=action_type,
            params=params,
            channel=channel,
            thread_ts=thread_ts,
            requested_by=requested_by,
            region=region,
            devices=devices or [],
        )
        logger.info("Pending action created: %s (%s)", action_id, action_type)
        return action_id

    def approve(self, action_id: str, approver_id: str) -> Optional[ActionRecord]:
        record = self.get_action(action_id)
        if not record or record.status != "pending":
            return None
        record.status = "approved"
        logger.info("Action %s approved by %s", action_id, approver_id)
        return record

    def deny(self, action_id: str, denier_id: str) -> Optional[ActionRecord]:
        record = self.get_action(action_id)
        if not record or record.status != "pending":
            return None
        record.status = "denied"
        logger.info("Action %s denied by %s", action_id, denier_id)
        return record

    def complete(self, action_id: str, result: dict) -> None:
        record = self._store.get(action_id)
        if record:
            record.status = "completed" if result.get("success") else "failed"
            record.result = result

    def get_action(self, action_id: str) -> Optional[ActionRecord]:
        record = self._store.get(action_id)
        if not record:
            return None
        if record.status == "pending" and (time.time() - record.requested_at) > ACTION_TTL_SECONDS:
            record.status = "expired"
            logger.info("Action %s expired", action_id)
        return record

    def cleanup_expired(self) -> None:
        for record in self._store.values():
            if record.status == "pending" and (time.time() - record.requested_at) > ACTION_TTL_SECONDS:
                record.status = "expired"


approval_manager = ApprovalManager()
