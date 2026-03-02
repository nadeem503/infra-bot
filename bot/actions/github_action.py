"""GitHub action: trigger GitHub Actions workflow dispatch."""
import requests

from config import settings
from .base_action import BaseAction

GITHUB_API = "https://api.github.com"


class GitHubAction(BaseAction):
    action_type = "github_workflow"

    def execute(self) -> dict:
        repo = self.params.get("repo", "")
        workflow = self.params.get("workflow", "")
        ref = self.params.get("ref", "main")
        inputs = self.params.get("inputs", {})

        if not settings.GITHUB_TOKEN:
            return {
                "success": False,
                "message": "GitHub token not configured (GITHUB_TOKEN env var missing)",
                "details": {},
            }
        if not repo or not workflow:
            return {"success": False, "message": "repo and workflow are required", "details": {}}

        url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
        headers = {
            "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = requests.post(
                url, json={"ref": ref, "inputs": inputs}, headers=headers, timeout=30
            )
            return {
                "success": response.status_code == 204,
                "message": f"Workflow `{workflow}` in `{repo}` dispatched: HTTP {response.status_code}",
                "details": {
                    "repo": repo,
                    "workflow": workflow,
                    "ref": ref,
                    "status_code": response.status_code,
                },
            }
        except requests.RequestException as exc:
            return {
                "success": False,
                "message": f"GitHub API request failed: {type(exc).__name__}",
                "details": {"error_type": type(exc).__name__},
            }
