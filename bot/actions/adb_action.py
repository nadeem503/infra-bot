"""ADB action: run ADB commands for Android device management."""
import shlex
import subprocess

from .base_action import BaseAction

ALLOWED_ADB_COMMANDS: frozenset[str] = frozenset({
    "devices",
    "kill-server",
    "start-server",
    "reboot",
    "reboot recovery",
    "shell getprop",
    "shell df",
    "shell ps",
    "shell am force-stop",
    "shell dumpsys battery",
    "shell input keyevent 26",
    "logcat -d",
})


class ADBAction(BaseAction):
    action_type = "adb_restart"

    def execute(self) -> dict:
        udid = self.params.get("udid", "")
        command = self.params.get("command", "devices")

        normalized = command.strip().lower()
        if not any(
            normalized == a.lower() or normalized.startswith(a.lower())
            for a in ALLOWED_ADB_COMMANDS
        ):
            return {
                "success": False,
                "message": f"ADB command `{command}` is not in the allowlist.",
                "details": {"allowed": sorted(ALLOWED_ADB_COMMANDS)},
            }

        full_cmd = f"adb -s {shlex.quote(udid)} {command}" if udid else f"adb {command}"

        try:
            result = subprocess.run(
                full_cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            return {
                "success": result.returncode == 0,
                "message": f"ADB `{command}` on `{udid or 'all devices'}`: exit={result.returncode}",
                "details": {
                    "stdout": result.stdout.strip()[:500],
                    "stderr": result.stderr.strip()[:200],
                    "return_code": result.returncode,
                    "udid": udid,
                },
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "ADB command timed out after 30s",
                "details": {"udid": udid, "command": command},
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"ADB command failed: {type(exc).__name__}",
                "details": {"error_type": type(exc).__name__},
            }
