"""Jenkins action: resolve job name, show parameters, trigger via REST API."""
from __future__ import annotations

import json
import re

import requests

from config import settings
from utils.jenkins_client import get_job_params, search_job
from utils.logger import get_logger
from .base_action import BaseAction

logger = get_logger(__name__)


def _map_params_with_ai(jenkins_param_defs: list[dict], user_params: dict) -> dict:
    """Map user-provided values to exact Jenkins parameter names using Claude CLI (Gemini fallback).

    Claude reads the Jenkins param names + descriptions and figures out the mapping itself.
    No hardcoded field hints — works for any job with any param naming convention.
    """
    # Build Jenkins param summary
    param_list = "\n".join(
        f"  - {p['name']}: {p['description'] or 'no description'} (default: {p['default']!r})"
        for p in jenkins_param_defs if p["name"]
    )

    # Pass ALL user params as-is — let the AI figure out what maps where
    user_values = {k: v for k, v in user_params.items()
                   if v not in (None, "", [], {}) and k not in ("summary", "description")}

    if not user_values:
        return {}

    prompt = f"""You are mapping user-provided values to Jenkins job parameters.

Jenkins job parameters (name: description, default):
{param_list}

User provided these values (in any format):
{json.dumps(user_values, indent=2)}

Task: Use semantic understanding to map the user's values to the correct Jenkins parameter names.
- Only map values the user explicitly provided — do NOT invent or assume values
- Use the EXACT Jenkins parameter name as defined above (case-sensitive)
- If a user value could match multiple params, pick the most semantically correct one
- Ignore user values that clearly don't correspond to any Jenkins param

Return ONLY a valid JSON object. No explanation, no markdown fences.
Example: {{"HOST_IP": "10.1.1.1 10.1.1.2", "ENV": "prod", "TAGS": "all"}}"""

    def _parse(text: str) -> dict:
        text = re.sub(r'^```[a-z]*\n?', '', text.strip())
        text = re.sub(r'\n?```$', '', text.strip())
        result = json.loads(text)
        if isinstance(result, dict):
            return {k: str(v) for k, v in result.items() if v not in (None, "", [])}
        return {}

    # --- Primary: Claude CLI ---
    try:
        import os, subprocess  # noqa: E401, PLC0415
        env = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/Users/ltadmin"),
            "USER": os.environ.get("USER", "ltadmin"),
            "LOGNAME": os.environ.get("LOGNAME", "ltadmin"),
        }
        result = subprocess.run(
            ["/opt/homebrew/bin/claude", "-p", prompt, "--model", "claude-sonnet-4-6"],
            capture_output=True, text=True, timeout=20, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            mapped = _parse(result.stdout.strip())
            logger.info("Claude CLI param mapping: %s", mapped)
            return mapped
        logger.warning("Claude CLI param mapping failed (rc=%d): %s", result.returncode, result.stderr[:100])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude CLI param mapping error: %s — trying Gemini fallback", exc)

    # --- Fallback: Gemini ---
    try:
        from google import genai  # noqa: PLC0415
        from config import settings as _s  # noqa: PLC0415
        if not _s.GEMINI_API_KEY:
            return {}
        client = genai.Client(api_key=_s.GEMINI_API_KEY)
        resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        mapped = _parse(resp.text)
        logger.info("Gemini param mapping fallback: %s", mapped)
        return mapped
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini param mapping also failed: %s", exc)

    return {}


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

        # --- build params: Jenkins defaults merged with AI-mapped user values ---
        jenkins_param_defs = get_job_params(job_name)
        # Start with Jenkins defaults
        job_params: dict = {p["name"]: p["default"] for p in jenkins_param_defs if p["name"]}

        if jenkins_param_defs:
            # Let AI map the user's values to the exact Jenkins param names
            ai_mapped = _map_params_with_ai(jenkins_param_defs, self.params)
            # AI values override Jenkins defaults (user intent > defaults)
            job_params.update(ai_mapped)
        else:
            # No param definitions from Jenkins — fall back to job_params from Claude directly
            overrides = self.params.get("job_params") or {}
            if isinstance(overrides, dict):
                job_params.update({k: str(v) for k, v in overrides.items() if v is not None and v != ""})

        # Remove empty values
        job_params = {k: v for k, v in job_params.items() if v not in (None, "", [])}

        logger.info("Jenkins resolved: %s → %s | params=%s", raw_name, job_name, job_params)
        return job_name, job_params, None
