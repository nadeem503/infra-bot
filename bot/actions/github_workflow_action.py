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

import time

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

    def _get_latest_run_url(self, headers: dict, owner: str, repo: str) -> str:
        """Poll for the actual run URL after a successful dispatch.

        The dispatch API returns 204 with no body, so we fetch the most
        recently created workflow_dispatch run (created within the last 30s).
        Retries up to 3 times with a 3s gap to allow GitHub to register the run.
        """
        runs_api = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/actions/workflows"
            f"/{self.workflow_file}/runs?event=workflow_dispatch&per_page=5"
        )
        cutoff = int(time.time()) - 30  # only runs created in the last 30 seconds

        for attempt in range(3):
            if attempt > 0:
                time.sleep(3)
            try:
                r = requests.get(runs_api, headers=headers, timeout=10)
                if r.status_code != 200:
                    break
                runs = r.json().get("workflow_runs", [])
                for run in runs:
                    # created_at is ISO 8601 e.g. "2026-04-14T10:47:49Z"
                    created_ts = int(
                        time.mktime(
                            time.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                        )
                    ) - time.timezone  # convert local mktime → UTC epoch
                    if created_ts >= cutoff:
                        logger.info("Found new run: %s", run["html_url"])
                        return run["html_url"]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Run URL poll attempt %d failed: %s", attempt + 1, exc)

        # Fall back to the workflow listing page
        return (
            f"https://github.com/{_MIGRATIONS_REPO}/actions/workflows"
            f"/{self.workflow_file}"
        )

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
            # Dispatch accepted — now fetch the actual run URL (with retries)
            runs_url = self._get_latest_run_url(headers, owner, repo)
            logger.info("Workflow dispatched successfully: %s → %s", self.workflow_file, runs_url)
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
