"""Abstract base class for all infrastructure actions."""
import abc

from utils.logger import get_logger, audit_log
from utils.ssh_exec import ssh_exec as _ssh_exec


def ssh_run(host: str, cmd: str) -> tuple[int, str, str]:
    """Run a command on a remote host via SSH. Returns (exit_code, stdout, stderr)."""
    r = _ssh_exec(host, cmd)
    return r["exit_code"], r["output"], r["error"]


class BaseAction(abc.ABC):
    """Subclasses implement ``execute()``; call ``run()`` for logging + error handling."""

    action_type: str = "base"

    def __init__(
        self,
        params: dict,
        triggered_by: str,
        channel: str,
        region: str = "unknown",
    ) -> None:
        self.params = params
        self.triggered_by = triggered_by
        self.channel = channel
        self.region = region
        self.logger = get_logger(self.__class__.__name__)
        self.devices: list = params.get("devices", [])

    @abc.abstractmethod
    def execute(self) -> dict:
        """Execute the action.

        Returns dict with keys:
          - success (bool)
          - message (str) — Slack-safe, no credentials
          - details (dict)
        """

    def dry_run(self) -> str:
        """Return a human-readable preview of what this action would do.

        No network or SSH calls are made here.
        Override in subclasses for action-specific previews.
        """
        device_str = ", ".join(f"`{d}`" for d in self.devices) if self.devices else "no device"
        return f"`{self.action_type}` on {device_str} (region: {self.region})"

    def run(self) -> dict:
        """Public entry point: audit-logs before/after and wraps exceptions."""
        audit_log(
            action_type=self.action_type,
            triggered_by=self.triggered_by,
            channel=self.channel,
            devices=self.devices,
            region=self.region,
            params=self.params,
            status="executing",
        )
        try:
            result = self.execute()
            audit_log(
                action_type=self.action_type,
                triggered_by=self.triggered_by,
                channel=self.channel,
                devices=self.devices,
                region=self.region,
                params=self.params,
                status="completed" if result.get("success") else "failed",
                result_summary=result.get("message", ""),
            )
            return result
        except PermissionError:
            raise  # callers handle this separately
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Action %s error: %s", self.action_type, exc)
            audit_log(
                action_type=self.action_type,
                triggered_by=self.triggered_by,
                channel=self.channel,
                devices=self.devices,
                region=self.region,
                params=self.params,
                status="error",
                result_summary=str(exc),
            )
            return {
                "success": False,
                "message": f"Action failed: {type(exc).__name__}",
                "details": {"error_type": type(exc).__name__},
            }
