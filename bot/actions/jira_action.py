"""Jira action: create Jira tickets for infrastructure issues."""
import base64

import requests

from config import settings
from .base_action import BaseAction

JIRA_API_BASE = "https://api.atlassian.com/ex/jira"
PROJECT_KEY = "TE"
ISSUE_TYPE_ID = "10204"  # Simple Task
TEAM_FIELD_VALUE = "b79a27b6-de36-4381-8d60-0b0c3e6477a7"  # Platform Engineering


class JiraAction(BaseAction):
    action_type = "jira_ticket"

    def execute(self) -> dict:
        summary = self.params.get("summary", "Infra Issue")
        description = self.params.get("description", "") or summary
        priority = self.params.get("priority", "High")

        if not all([settings.JIRA_EMAIL, settings.JIRA_API_TOKEN]):
            return {
                "success": False,
                "message": "Jira credentials not configured (JIRA_EMAIL, JIRA_API_TOKEN missing)",
                "details": {},
            }

        credentials = base64.b64encode(
            f"{settings.JIRA_EMAIL}:{settings.JIRA_API_TOKEN}".encode()
        ).decode()
        url = f"{JIRA_API_BASE}/{settings.JIRA_CLOUD_ID}/rest/api/3/issue"

        payload = {
            "fields": {
                "project": {"key": PROJECT_KEY},
                "summary": summary,
                "issuetype": {"id": ISSUE_TYPE_ID},
                "assignee": {"id": settings.JIRA_ASSIGNEE_ID},
                "priority": {"name": priority},
                "customfield_10001": TEAM_FIELD_VALUE,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": description}],
                        }
                    ],
                },
            }
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            data = response.json()
            if response.status_code == 201:
                key = data.get("key", "UNKNOWN")
                return {
                    "success": True,
                    "message": f"Jira ticket created: *{key}*",
                    "details": {
                        "issue_key": key,
                        "issue_id": data.get("id"),
                        "url": f"https://lambdatest.atlassian.net/browse/{key}",
                    },
                }
            else:
                errors = data.get("errors", data.get("errorMessages", []))
                return {
                    "success": False,
                    "message": f"Jira ticket creation failed: HTTP {response.status_code}",
                    "details": {"errors": errors},
                }
        except requests.RequestException as exc:
            return {
                "success": False,
                "message": f"Jira API request failed: {type(exc).__name__}",
                "details": {"error_type": type(exc).__name__},
            }
