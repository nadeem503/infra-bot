"""Jenkins action: resolve job name, show parameters, trigger via REST API."""
from __future__ import annotations

import requests

from config import settings
from utils.jenkins_client import get_job_params, search_job
from utils.logger import get_logger
from .base_action import BaseAction

logger = get_logger(__name__)


class JenkinsAction(BaseAction):
    action_type = "jenkins_trigger"

    # ------------------------------------------------------------------
    # dry_run — called before the approval card is posted so the user
    # can see exactly which job and parameters will be used.
    # ------------------------------------------------------------------
    def dry_run(self) -> str:
        job_name, job_params, error = self._resolve()
        if error:
            return f":warning: {error}"

        lines = [f":jenkins: *Job:* `{job_name}`"]
        if job_params:
            lines.append("*Parameters:*")
            for k, v in job_params.items():
                lines.append(f"  • `{k}` = `{v}`")
        else:
            lines.append("_No parameters_")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # execute — triggered after user clicks ✅ Confirm
    # ------------------------------------------------------------------
    def execute(self) -> dict:
        job_name, job_params, error = self._resolve()
        if error:
            return {"success": False, "message": error, "details": {}}

        if not settings.JENKINS_URL:
            return {
                "success": False,
                "message": ":lock: Jenkins URL not configured (`JENKINS_URL` missing in bot config)",
                "details": {},
            }

        url = f"{settings.JENKINS_URL.rstrip('/')}/job/{job_name}/buildWithParameters"
        try:
            response = requests.post(
                url,
                params=job_params,
                auth=(settings.JENKINS_USER, settings.JENKINS_API_TOKEN),
                timeout=30,
            )
            success = response.status_code in (200, 201)
            return {
                "success": success,
                "message": (
                    f":white_check_mark: Jenkins job `{job_name}` triggered successfully"
                    if success else
                    f":x: Jenkins job `{job_name}` failed: HTTP {response.status_code}"
                ),
                "details": {"job_name": job_name, "status_code": response.status_code},
            }
        except requests.RequestException as exc:
            return {
                "success": False,
                "message": f":x: Jenkins API request failed: {type(exc).__name__}",
                "details": {"error_type": type(exc).__name__},
            }

    # ------------------------------------------------------------------
    # _resolve — find the real job name and merge parameters
    # ------------------------------------------------------------------
    def _resolve(self) -> tuple[str, dict, str | None]:
        """Returns (job_name, job_params_dict, error_message_or_None)."""
        raw_name = self.params.get("job_name", "").strip()
        if not raw_name:
            return "", {}, "No Jenkins job name or description provided"

        if not settings.JENKINS_URL:
            return "", {}, ":lock: Jenkins URL not configured (`JENKINS_URL` missing in bot config)"

        # --- resolve job name ---
        job_name = search_job(raw_name)
        if not job_name:
            return "", {}, (
                f"Could not find a Jenkins job matching `{raw_name}`. "
                "Please provide the exact job name."
            )

        # --- build params: Jenkins defaults → overridden by Claude's params ---
        jenkins_param_defs = get_job_params(job_name)
        # Start with Jenkins defaults
        job_params: dict = {p["name"]: p["default"] for p in jenkins_param_defs if p["name"]}

        # Override with whatever Claude extracted (host_ips, ENV, tags, etc.)
        overrides = self.params.get("job_params") or {}
        if isinstance(overrides, dict):
            job_params.update({k: str(v) for k, v in overrides.items() if v is not None and v != ""})

        # Common field aliases: host → HOST_IP, environment → ENV, etc.
        _aliases = {
            "host_ips":    "HOST_IP",
            "host":        "HOST_IP",
            "environment": "ENV",
            "env":         "ENV",
            "tags":        "Tags",
            "udid":        "UDID",
        }
        for src, dst in _aliases.items():
            val = self.params.get(src)
            if val and dst not in job_params:
                job_params[dst] = str(val) if not isinstance(val, list) else ",".join(val)

        # Remove empty values
        job_params = {k: v for k, v in job_params.items() if v not in (None, "", [])}

        logger.info("Jenkins resolved: %s → %s | params=%s", raw_name, job_name, job_params)
        return job_name, job_params, None
