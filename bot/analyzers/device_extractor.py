"""Device extractor: parses UDID, IP addresses, and hostnames from message text."""
import re
from typing import NamedTuple

from utils.logger import get_logger

logger = get_logger(__name__)

# 40-char hex UDID (Android / iOS)
UDID_PATTERN = re.compile(r'\b([0-9a-fA-F]{40})\b')

# IPv4 addresses
IP_PATTERN = re.compile(
    r'\b((?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}'
    r'(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))\b'
)

# Short device hostnames: device-001, dc-node-12, pod-xyz, etc.
SHORT_HOSTNAME_PATTERN = re.compile(
    r'\b((?:device|node|host|server|dc|vm|pod)[-_][a-zA-Z0-9]{1,20}'
    r'(?:[-_][a-zA-Z0-9]{1,20})*)\b',
    re.IGNORECASE,
)

# FQDN fallback
FQDN_PATTERN = re.compile(
    r'\b([a-zA-Z][a-zA-Z0-9\-]{2,}(?:\.[a-zA-Z0-9\-]+)*\.[a-zA-Z]{2,})\b'
)
FQDN_SKIP = {"slack.com", "github.com", "atlassian.net", "google.com"}


class DeviceInfo(NamedTuple):
    type: str   # 'udid' | 'ip' | 'hostname'
    value: str


class DeviceExtractor:
    def extract(self, text: str) -> list[DeviceInfo]:
        """Extract all device identifiers; returns a deduplicated list."""
        devices: list[DeviceInfo] = []
        seen: set[str] = set()

        def add(dtype: str, value: str) -> None:
            if value not in seen:
                devices.append(DeviceInfo(type=dtype, value=value))
                seen.add(value)

        for m in UDID_PATTERN.finditer(text):
            add("udid", m.group(1))
        for m in IP_PATTERN.finditer(text):
            add("ip", m.group(1))
        for m in SHORT_HOSTNAME_PATTERN.finditer(text):
            add("hostname", m.group(1))

        # FQDN fallback only when nothing else was found
        if not devices:
            for m in FQDN_PATTERN.finditer(text):
                val = m.group(1)
                if val.lower() not in FQDN_SKIP:
                    add("hostname", val)

        logger.debug("Extracted %d device identifiers", len(devices))
        return devices

    def format_devices(self, devices: list[DeviceInfo]) -> str:
        if not devices:
            return "No devices identified"
        return ", ".join(f"`{d.value}`" for d in devices)
