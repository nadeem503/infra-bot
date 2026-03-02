"""Region detector: identifies DC region from message text using regions.yaml."""
import re
from typing import Optional

from utils.config_loader import get_regions
from utils.logger import get_logger

logger = get_logger(__name__)


class RegionDetector:
    def __init__(self) -> None:
        self._regions: Optional[dict] = None
        self._compiled: Optional[dict] = None

    @property
    def regions(self) -> dict:
        if self._regions is None:
            self._regions = get_regions()
        return self._regions

    @property
    def compiled_patterns(self) -> dict:
        if self._compiled is None:
            self._compiled = {
                slug: [re.compile(p, re.IGNORECASE) for p in cfg.get("patterns", [])]
                for slug, cfg in self.regions.items()
            }
        return self._compiled

    def detect(self, text: str) -> Optional[str]:
        """Return the first matching region slug, or None."""
        for slug, patterns in self.compiled_patterns.items():
            for pattern in patterns:
                if pattern.search(text):
                    logger.debug("Region detected: %s", slug)
                    return slug
        return None

    def get_display_name(self, region_slug: Optional[str]) -> str:
        if not region_slug:
            return "Unknown"
        return self.regions.get(region_slug, {}).get("display_name", region_slug.upper())

    def get_timezone(self, region_slug: Optional[str]) -> str:
        if not region_slug:
            return "UTC"
        return self.regions.get(region_slug, {}).get("timezone", "UTC")
