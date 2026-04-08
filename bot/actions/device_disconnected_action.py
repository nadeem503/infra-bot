"""Device Disconnected Action: handles "MISMATCH: DB=N, Device=device not found".

When DB shows a device but ADB/SSH reports "device not found":
  1. Skip the mismatch record (don't treat as a normal issue)
  2. Classify as device_disconnected
  3. Attempt ADB reconnect once (kill-server + start-server + connect)
  4. If reconnect succeeds → report recovered
  5. If still not found → report as physically disconnected (needs manual check)

This is the only action class with built-in retry logic.
"""
from __future__ import annotations

import shlex
import subprocess
import time

from .base_action import BaseAction

RECONNECT_WAIT_SECONDS = 5


class DeviceDisconnectedAction(BaseAction):
    action_type = "device_disconnected"

    def dry_run(self) -> str:
        udid = self.params.get("udid", "no-udid")
        return (
            f"1. `adb kill-server` → `adb start-server`\n"
            f"2. `adb -s {udid} get-state` (retry once after {RECONNECT_WAIT_SECONDS}s)\n"
            f"3. Report recovered or flag for physical inspection"
        )

    def execute(self) -> dict:
        udid = self.params.get("udid", "").strip()
        if not udid:
            return {
                "success": False,
                "message": "No UDID provided for disconnected device action",
                "details": {},
            }

        self.logger.info("Device disconnected action for UDID: %s", udid)

        # Step 1: check if device is actually present in adb devices
        initial_state = self._get_adb_state(udid)
        if initial_state == "device":
            return {
                "success": True,
                "message": f":white_check_mark: `{udid}` is reachable via ADB — possible transient mismatch, DB may need sync",
                "details": {"udid": udid, "state": "device", "retry": False},
            }

        self.logger.info("ADB state for %s: %s — attempting reconnect", udid, initial_state)

        # Step 2: restart ADB server and wait
        self._restart_adb_server()
        time.sleep(RECONNECT_WAIT_SECONDS)

        # Step 3: retry state check
        retry_state = self._get_adb_state(udid)
        if retry_state == "device":
            return {
                "success": True,
                "message": (
                    f":white_check_mark: `{udid}` recovered after ADB server restart\n"
                    f"_Retry succeeded — device is back online_"
                ),
                "details": {"udid": udid, "state": retry_state, "retry": True},
            }

        # Step 4: device still not found — flag for manual check
        return {
            "success": False,
            "message": (
                f":warning: `{udid}` still not found after ADB reconnect attempt\n"
                f"ADB state: `{retry_state or 'not listed'}` — "
                f"*physical inspection required* (check cable / power / rack position)"
            ),
            "details": {
                "udid": udid,
                "initial_state": initial_state,
                "retry_state": retry_state,
                "retry": True,
                "action_required": "physical_inspection",
            },
        }

    def _get_adb_state(self, udid: str) -> str:
        """Return ADB state string: 'device', 'offline', 'unauthorized', or empty."""
        try:
            result = subprocess.run(
                f"adb -s {shlex.quote(udid)} get-state",
                shell=True, capture_output=True, text=True, timeout=15,
            )
            return result.stdout.strip() or result.stderr.strip() or "not_found"
        except subprocess.TimeoutExpired:
            return "timeout"
        except Exception:  # noqa: BLE001
            return "error"

    def _restart_adb_server(self) -> None:
        try:
            subprocess.run("adb kill-server", shell=True, capture_output=True, timeout=10)
            subprocess.run("adb start-server", shell=True, capture_output=True, timeout=15)
            self.logger.info("ADB server restarted")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("ADB server restart failed: %s", exc)
