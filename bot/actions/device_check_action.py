"""Device connectivity check action.

Immediately SSHes to the host (internal DC network) and runs:
  adb devices        — list connected devices
  adb -s <UDID> get-state — specific device status (if UDID given)

No approval workflow — read-only diagnostic, returns result directly.
"""
from __future__ import annotations

from utils.logger import get_logger
from utils.ssh_exec import ssh_exec

logger = get_logger(__name__)


class DeviceCheckAction:
    """Runs ADB connectivity check on a DC host and returns a Slack-ready reply."""

    def execute(self, host: str, udid: str = "") -> str:
        if not host:
            return ":warning: No host IP found in your message. Try: `@infra-bot 10.151.2.22,S499NNSGU8GUW8O7 check if connected`"

        # Run adb devices on the host
        result = ssh_exec(host, "adb devices")

        if not result["success"] and result["exit_code"] == -1:
            return (
                f":x: SSH connection to `{host}` failed\n"
                f"```{result['error'][:300]}```"
            )

        adb_output = result["output"]

        # If we have a specific UDID/serial, check it
        if udid:
            lines = adb_output.splitlines()
            device_line = next((l for l in lines if udid in l), None)

            if device_line:
                parts = device_line.strip().split("\t")
                status = parts[1].strip() if len(parts) > 1 else "found"
                icon = ":white_check_mark:" if status == "device" else ":warning:"
                state_str = {
                    "device":       "connected & authorized",
                    "unauthorized": "connected but UNAUTHORIZED",
                    "offline":      "OFFLINE",
                    "no permissions": "NO PERMISSIONS",
                }.get(status, status)
                reply = (
                    f"{icon} *Device `{udid}`* — `{state_str}` on host `{host}`\n"
                    f"```{adb_output}```"
                )
            else:
                # Device not in adb devices list — try get-state for more info
                gs = ssh_exec(host, f"adb -s {udid} get-state")
                gs_out = gs["output"] or gs["error"] or "not found"
                reply = (
                    f":x: *Device `{udid}`* — NOT found in `adb devices` on host `{host}`\n"
                    f"```{adb_output}```\n"
                    f"get-state: `{gs_out.strip()[:100]}`"
                )
        else:
            # No specific UDID — just return all devices
            if adb_output and "List of devices attached" in adb_output:
                device_lines = [l for l in adb_output.splitlines() if "\t" in l]
                count = len(device_lines)
                icon = ":white_check_mark:" if count > 0 else ":warning:"
                reply = (
                    f"{icon} *ADB devices on `{host}`* — {count} device(s) attached\n"
                    f"```{adb_output}```"
                )
            else:
                reply = (
                    f":information_source: *ADB output from `{host}`*\n"
                    f"```{adb_output or '(empty)'}```"
                )

        logger.info("device_check host=%s udid=%s success=%s", host, udid, result["success"])
        return reply
