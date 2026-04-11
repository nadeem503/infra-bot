"""Shared Redis client singleton."""
from __future__ import annotations

import threading

import redis
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_client: redis.Redis | None = None
_lock = threading.Lock()


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                url = settings.REDIS_URL or "redis://localhost:6379/0"
                _client = redis.from_url(url, decode_responses=True)
                safe = url.split("@")[-1] if "@" in url else url
                logger.info("Redis connected: %s", safe)
    return _client
