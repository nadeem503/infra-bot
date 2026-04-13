"""GitHub Actions workflow_dispatch trigger — base class.

Subclasses specify `workflow_file` and call `_trigger_workflow(inputs)`.
The GitHub token (`GITHUB_TOKEN` in settings) must have `actions:write` scope
on the LambdatestIncPrivate/migrations repo.

Why GitHub Actions instead of running SQL directly?
- The migrations repo already has DB secrets, environment protection gates,
  row-limit guards, audit trails (step summary), and deallocation API logic.
- Triggering via workflow_dispatch re-uses all of that safely without
  duplicating credentials in the bot.
"""
from __future__ import annotations

import requests

from bot.actions.base_action import BaseAction
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_GITHUB_API      = "https://api.github.com"
_MIGRATIONS_REPO = "LambdatestIncPrivate/migrations"


class GitHubWorkflowAction(BaseAction):
    """Base class for actions that trigger GitHub Actions workflow_dispatch events.

    Subclasses must set:
      workflow_file (str) — e.g. "realdevice-db-lmds-dispose-update.yml"
    """

    workflow_file: str = ""

    def _trigger_workflow(self, inputs: dict, ref: str = "stage") -> dict:
        """POST a workflow_dispatch event to GitHub.

        Returns: {"success": bool, "message": str, "runs_url": str (on success)}
        """
        token = settings.GITHUB_TOKEN
        if not token:
            return {
                "success": False,
                "message": ":lock: `GITHUB_TOKEN` not set in bot config — cannot trigger workflow",
                "details": {},
            }
        if not self.workflow_file:
            return {
                "success": False,
                "message": "workflow_file not set on action class",
                "details": {},
            }

        owner, repo = _MIGRATIONS_REPO.split("/")
        url = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/actions/workflows"
            f"/{self.workflow_file}/dispatches"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload = {"ref": ref, "inputs": inputs}

        logger.info(
            "Triggering workflow %s ref=%s inputs=%s",
            self.workflow_file, ref, inputs,
        )
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
        except Exception as exc:  # noqa: BLE001
            logger.error("GitHub API request failed: %s", exc)
            return {
                "success": False,
                "message": f"GitHub API request failed: {exc}",
                "details": {},
            }

        if resp.status_code == 204:
            runs_url = (
                f"https://github.com/{_MIGRATIONS_REPO}/actions/workflows"
                f"/{self.workflow_file}"
            )
            logger.info("Workflow dispatched successfully: %s", self.workflow_file)
            return {
                "success": True,
                "message": "Workflow triggered",
                "runs_url": runs_url,
                "details": {},
            }

        # 422 = validation error (bad inputs), 404 = workflow not found, 403 = no permission
        logger.error(
            "Workflow dispatch failed: %s %s", resp.status_code, resp.text[:300]
        )
        return {
            "success": False,
            "message": (
                f":x: GitHub Actions returned `{resp.status_code}` — "
                f"{resp.json().get('message', resp.text[:150]) if resp.text else 'no detail'}"
            ),
            "details": {"status_code": resp.status_code},
        }
