"""SSH reboot action: reboots a DC host or an iOS device via SSH.

Host-type aware:
  macOS (iOS devices): ssh host → idevicediagnostics -u <UDID> restart
  Ubuntu (Android):    ssh host → sudo reboot

Uses ssh_exec (direct SSH from bot host via sshpass/paramiko), same as other actions.
The old paramiko+bastion path is removed — bot host 10.151.2.248 has direct SSH access.
"""
from __future__ import annotations

import re

from utils.ssh_exec import ssh_exec
from .base_action import BaseAction

_IOS_UDID_NEW = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{16}$')
_IOS_UDID_OLD = re.compile(r'^[0-9a-fA-F]{40}$')
_IDEVICEDIAGNOSTICS = "/opt/homebrew/bin/idevicediagnostics"


def _is_ios_udid(udid: str) -> bool:
    return bool(_IOS_UDID_NEW.match(udid) or _IOS_UDID_OLD.match(udid))


def _detect_host_os(host: str) -> str:
    """Return 'macos', 'ubuntu', or 'unknown' via SSH uname -s."""
    result = ssh_exec(host, "uname -s")
    if result["exit_code"] == 0:
        out = result["output"].strip().lower()
        if "darwin" in out:
            return "macos"
        if "linux" in out:
            return "ubuntu"
    return "unknown"


class SSHAction(BaseAction):
    action_type = "ssh_reboot"

    def execute(self) -> dict:
        target_host = self.params.get("host", "")
        udid = self.params.get("udid", "")

        if not target_host:
            return {"success": False, "message": "No target host specified", "details": {}}

        # Determine host OS — primary: uname -s; fallback: UDID format
        host_os = _detect_host_os(target_host)
        if host_os == "unknown":
            host_os = "macos" if (udid and _is_ios_udid(udid)) else "ubuntu"
            self.logger.warning("ssh_reboot: uname failed for %s, inferred %s from UDID", target_host, host_os)

        if host_os == "macos":
            return self._reboot_ios(target_host, udid)
        else:
            return self._reboot_android(target_host)

    def _reboot_ios(self, host: str, udid: str) -> dict:
        """Reboot iOS device via idevicediagnostics on macOS host."""
        if not udid:
            return {
                "success": False,
                "message": f":warning: macOS host `{host}` but no UDID — cannot reboot specific device",
                "details": {"host": host},
            }
        self.logger.info("ssh_reboot iOS: host=%s udid=%s", host, udid)
        result = ssh_exec(host, f"{_IDEVICEDIAGNOSTICS} -u {udid} restart")
        if result["exit_code"] == -1:
            return {
                "success": False,
                "message": f"SSH to `{host}` failed: {result['error'][:100]}",
                "details": result,
            }
        success = result["exit_code"] == 0
        if success:
            msg = f":arrows_counterclockwise: iOS device `{udid}` reboot triggered on `{host}`"
        else:
            msg = f":x: idevicediagnostics failed on `{host}`: {result['output'][:100] or result['error'][:100]}"
        return {"success": success, "message": msg, "details": result}

    def _reboot_android(self, host: str) -> dict:
        """Reboot Ubuntu host via sudo reboot."""
        self.logger.info("ssh_reboot Android/Ubuntu: host=%s", host)
        result = ssh_exec(host, "sudo reboot")
        # sudo reboot causes connection drop (exit_code -1 or non-zero) — treat as success
        if result["exit_code"] in (0, -1) and "timed out" not in result.get("error", "").lower():
            return {
                "success": True,
                "message": f":arrows_counterclockwise: Reboot triggered on `{host}`",
                "details": result,
            }
        return {
            "success": False,
            "message": f":x: Reboot failed on `{host}`: {result['error'][:100]}",
            "details": result,
        }

    def dry_run(self) -> str:
        host = self.params.get("host", "")
        udid = self.params.get("udid", "")
        if udid and _is_ios_udid(udid):
            return f"`idevicediagnostics -u {udid} restart` on macOS host `{host}`"
        return f"`sudo reboot` on Ubuntu host `{host}`"
