"""Issue detector: classifies issue type from message text using keywords.yaml."""
from typing import Optional

from utils.config_loader import get_keywords
from utils.logger import get_logger

logger = get_logger(__name__)


class IssueDetector:
    def __init__(self) -> None:
        self._keywords: Optional[dict] = None

    @property
    def keywords(self) -> dict:
        if self._keywords is None:
            self._keywords = get_keywords()
        return self._keywords

    def detect(self, text: str) -> Optional[str]:
        """Return the primary issue category name, or None."""
        results = self.detect_all(text)
        return results[0]["category"] if results else None

    def detect_all(self, text: str) -> list[dict]:
        """Return all matching categories sorted by match count (descending)."""
        lowered = text.lower()
        results = []
        for category, config in self.keywords.items():
            kw_list = config.get("keywords", [])
            count = sum(1 for kw in kw_list if kw in lowered)
            if count > 0:
                results.append({
                    "category": category,
                    "severity": config.get("severity", "medium"),
                    "auto_action": config.get("auto_action", "device_status"),
                    "match_count": count,
                })
        results.sort(key=lambda x: x["match_count"], reverse=True)
        if results:
            logger.debug(
                "Issue detected: %s (%d keywords matched)",
                results[0]["category"], results[0]["match_count"],
            )
        return results

    def get_severity(self, category: str) -> str:
        return self.keywords.get(category, {}).get("severity", "medium")

    def get_auto_action(self, category: str) -> str:
        return self.keywords.get(category, {}).get("auto_action", "device_status")

    def get_issue_from_action(self, action_type: str) -> Optional[str]:
        """Reverse lookup: given action_type, return the issue category that maps to it."""
        for category, config in self.keywords.items():
            if config.get("auto_action") == action_type:
                return category
        return None
