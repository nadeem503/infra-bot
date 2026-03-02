"""Jenkins action: trigger Jenkins jobs via REST API."""
import requests

from config import settings
from .base_action import BaseAction


class JenkinsAction(BaseAction):
    action_type = "jenkins_trigger"

    def execute(self) -> dict:
        job_name = self.params.get("job_name", "")
        job_params = self.params.get("job_params", {})

        if not settings.JENKINS_URL:
            return {
                "success": False,
                "message": "Jenkins URL not configured (JENKINS_URL env var missing)",
                "details": {},
            }
        if not job_name:
            return {"success": False, "message": "No Jenkins job name specified", "details": {}}

        url = f"{settings.JENKINS_URL.rstrip('/')}/job/{job_name}/buildWithParameters"
        try:
            response = requests.post(
                url,
                params=job_params,
                auth=(settings.JENKINS_USER, settings.JENKINS_API_TOKEN),
                timeout=30,
            )
            return {
                "success": response.status_code in (200, 201),
                "message": f"Jenkins job `{job_name}` triggered: HTTP {response.status_code}",
                "details": {"job_name": job_name, "status_code": response.status_code},
            }
        except requests.RequestException as exc:
            return {
                "success": False,
                "message": f"Jenkins API request failed: {type(exc).__name__}",
                "details": {"error_type": type(exc).__name__},
            }
