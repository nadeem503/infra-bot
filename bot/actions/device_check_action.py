"""Device connectivity check action.

Immediately SSHes to the host (internal DC network) and runs:
  docker exec -it adbd_<UDID> adb devices  — Android container check
  adb -s <UDID> get-state                  — fallback state check

No approval workflow — read-only diagnostic, returns result directly.
"""
from __future__ import annotations

from utils.logger import get_logger
from utils.ssh_exec import ssh_exec

logger = get_logger(__name__)


class DeviceCheckAction:
    """Runs ADB connectivity check on a DC host and returns a Slack-ready reply."""

    def execute(self, host: str, udid: str = "", hosts: list | None = None, udids: list | None = None) -> str:
        """Check device connectivity.

        When a mapping list is passed (hosts + udids), checks each pair.
        Falls back to single host+udid mode for direct queries.
        """
        # Multi-pair mode: check each host,udid pair from a mapping list
        if hosts and len(hosts) > 1:
            return self._execute_multi(hosts, udids or [])

        if not host:
            return ":warning: No host IP found in your message. Try: `@infra-bot 10.151.2.22,S499NNSGU8GUW8O7 check if connected`"

        if not udid:
            return ":warning: No device UDID/serial found. Try: `@infra-bot 10.151.2.22,S499NNSGU8GUW8O7 check if connected`"

        cmd = f"docker exec -i adbd_{udid} adb devices 2>&1"
        result = ssh_exec(host, cmd)

        if not result["success"] and result["exit_code"] == -1:
            return (
                f":x: SSH connection to `{host}` failed\n"
                f"```{result['error'][:300]}```"
            )

        adb_output = result["output"]

        # Check if docker container exists / is running
        if "No such container" in adb_output or "Error" in adb_output:
            gs = ssh_exec(host, f"docker ps --filter name=adbd_{udid} --format '{{{{.Status}}}}'")
            container_status = gs["output"].strip() or "not found"
            return (
                f":x: *Device `{udid}`* — container `adbd_{udid}` issue on `{host}`\n"
                f"```{adb_output[:300]}```\n"
                f"Container status: `{container_status}`"
            )

        device_line = next((l for l in adb_output.splitlines() if udid in l), None)
        if device_line:
            parts = device_line.strip().split("\t")
            status = parts[1].strip() if len(parts) > 1 else "found"
            icon = ":white_check_mark:" if status == "device" else ":warning:"
            state_str = {
                "device":         "connected & authorized",
                "unauthorized":   "connected but UNAUTHORIZED",
                "offline":        "OFFLINE",
                "no permissions": "NO PERMISSIONS",
            }.get(status, status)
            reply = (
                f"{icon} *Device `{udid}`* — `{state_str}` on host `{host}`\n"
                f"```{adb_output}```"
            )
        else:
            gs = ssh_exec(host, f"docker exec -i adbd_{udid} adb -s {udid} get-state 2>&1")
            gs_out = gs["output"] or gs["error"] or "not found"
            reply = (
                f":x: *Device `{udid}`* — NOT found inside `adbd_{udid}` on `{host}`\n"
                f"```{adb_output}```\n"
                f"get-state: `{gs_out.strip()[:100]}`"
            )

        logger.info("device_check host=%s udid=%s success=%s", host, udid, result["success"])
        return reply

    def _execute_multi(self, hosts: list, udids: list) -> str:
        """Check connectivity for multiple host,udid pairs via docker exec."""
        pairs = list(zip(hosts, udids))[:20]  # cap at 20 pairs
        lines = [":mag: *Device connectivity check*\n"]
        ok = fail = 0

        for host_ip, serial in pairs:
            if not serial:
                lines.append(f":warning: `{host_ip}` — no UDID, skipped")
                continue

            cmd = f"docker exec -i adbd_{serial} adb devices 2>&1"
            res = ssh_exec(host_ip, cmd)

            if not res.get("success") and res.get("exit_code") == -1:
                lines.append(f":x: `{host_ip}` / `{serial}` — SSH failed")
                fail += 1
                continue

            adb_out = res.get("output", "")
            if "No such container" in adb_out or ("Error" in adb_out and serial not in adb_out):
                lines.append(f":x: `{host_ip}` / `{serial}` — container `adbd_{serial}` not running")
                fail += 1
                continue

            device_line = next((l for l in adb_out.splitlines() if serial in l), None)
            if device_line:
                parts = device_line.strip().split("\t")
                status = parts[1].strip() if len(parts) > 1 else "found"
                icon = ":white_check_mark:" if status == "device" else ":warning:"
                state = {"device": "authorized", "unauthorized": "UNAUTHORIZED",
                         "offline": "OFFLINE"}.get(status, status)
                lines.append(f"{icon} `{host_ip}` / `{serial}` — {state}")
                ok += 1
            else:
                lines.append(f":x: `{host_ip}` / `{serial}` — not found inside container")
                fail += 1

        lines.append(f"\n*Summary:* {ok} OK, {fail} failed out of {len(pairs)} pairs")
        logger.info("device_check multi: %d pairs, %d ok, %d fail", len(pairs), ok, fail)
        return "\n".join(lines)
