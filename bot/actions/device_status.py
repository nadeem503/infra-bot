"""Device status action: fetch device health, build info, OS state."""
import shlex
import subprocess

from config import settings
from .base_action import BaseAction

ADB_PROPS = {
    "ro.build.version.release": "android_version",
    "ro.build.id": "build_id",
    "ro.product.model": "model",
    "ro.product.manufacturer": "manufacturer",
    "ro.bootloader": "bootloader",
    "ro.build.fingerprint": "fingerprint",
}


class DeviceStatusAction(BaseAction):
    action_type = "device_status"

    def execute(self) -> dict:
        udid = self.params.get("udid", "")
        host = self.params.get("host", "")

        if not udid and not host:
            return {
                "success": False,
                "message": "No UDID or host specified for device status check",
                "details": {},
            }

        return self._adb_status(udid) if udid else self._ssh_status(host)

    def _adb_status(self, udid: str) -> dict:
        results: dict[str, str] = {}
        try:
            for prop, key in ADB_PROPS.items():
                proc = subprocess.run(
                    f"adb -s {shlex.quote(udid)} shell getprop {prop}",
                    shell=True, capture_output=True, text=True, timeout=10,
                )
                results[key] = proc.stdout.strip() or "unknown"
            return {
                "success": True,
                "message": f"Device status for `{udid}`",
                "details": {"udid": udid, "properties": results},
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"ADB status check failed for `{udid}`",
                "details": {"error_type": type(exc).__name__, "udid": udid},
            }

    def _ssh_status(self, host: str) -> dict:
        if not settings.BASTION_HOST:
            return {
                "success": False,
                "message": "Bastion host not configured",
                "details": {"host": host},
            }
        from bot.actions.ssh_action import SSHAction  # noqa: PLC0415

        return SSHAction(
            params={"host": host, "command": "uptime && df -h && free -h"},
            triggered_by=self.triggered_by,
            channel=self.channel,
            region=self.region,
        ).execute()
