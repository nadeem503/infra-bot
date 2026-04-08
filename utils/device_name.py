"""UDID → human-readable name lookup from config/devices.yaml.

Usage:
    from utils.device_name import get_device_name
    label = get_device_name("00008110-001234567890abcdef")
    # → "iPhone 14 Pro #3 (AP-rack-2)"  or the raw UDID if not mapped
"""
from __future__ import annotations

from pathlib import Path

import yaml

from utils.logger import get_logger

logger = get_logger(__name__)

_DEVICES_FILE = Path("config/devices.yaml")
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with _DEVICES_FILE.open() as f:
            data = yaml.safe_load(f) or {}
        _cache = data.get("devices", {}) or {}
    except FileNotFoundError:
        _cache = {}
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not load devices.yaml: %s", exc)
        _cache = {}
    return _cache


def get_device_name(device_id: str) -> str:
    """Return human name for device_id, or device_id itself if not mapped."""
    if not device_id:
        return device_id
    return _load().get(device_id, device_id)


def get_all_devices() -> dict:
    return _load()
