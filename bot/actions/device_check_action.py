"""Device connectivity check action.

Check flow per device:
  1. docker ps --filter name=adbd_<UDID>   — is container running?
  2. docker exec -i adbd_<UDID> adb -s <UDID> get-state  — targeted device state

No approval workflow — read-only diagnostic, returns result directly.
"""
from __future__ import annotations

from utils.logger import get_logger
from utils.ssh_exec import ssh_exec

logger = get_logger(__name__)

_STATE_MAP = {
    "device":       ("connected & authorized", ":white_check_mark:"),
    "offline":      ("OFFLINE",               ":warning:"),
    "unauthorized": ("UNAUTHORIZED",           ":warning:"),
    "bootloader":   ("in bootloader",          ":warning:"),
    "recovery":     ("in recovery mode",       ":warning:"),
}


def _check_single(host: str, udid: str) -> tuple[str, str]:
    """Return (icon, status_text) for one host+udid pair."""
    # Step 1: is the container running?
    ps = ssh_exec(host, f"docker ps --filter name=adbd_{udid} --format '{{{{.Status}}}}'")
    if ps["exit_code"] == -1:
        return ":x:", f"SSH to `{host}` failed: {ps['error'][:100]}"

    container_status = ps["output"].strip()
    if not container_status:
        # Container not found — check if it exists at all (stopped)
        all_ps = ssh_exec(host, f"docker ps -a --filter name=adbd_{udid} --format '{{{{.Status}}}}'")
        stopped_status = all_ps["output"].strip()
        if stopped_status:
            return ":x:", f"container `adbd_{udid}` is *stopped* (`{stopped_status}`)"
        return ":x:", f"container `adbd_{udid}` does *not exist* on `{host}`"

    # Step 2: get device state from inside the container
    gs = ssh_exec(host, f"docker exec -i adbd_{udid} adb -s {udid} get-state 2>&1")
    raw = (gs["output"] or "").strip()

    if "error" in raw.lower() or "no devices" in raw.lower() or not raw:
        return ":x:", f"container running but device not responding (`{raw[:80] or 'no output'}`)"

    state = raw.splitlines()[-1].strip()  # last line = state word
    icon, label = _STATE_MAP.get(state, (":information_source:", state))
    return icon, f"{label} (container: `{container_status}`)"


class DeviceCheckAction:
    """Runs ADB connectivity check on a DC host and returns a Slack-ready reply."""

    def execute(self, host: str, udid: str = "", hosts: list | None = None, udids: list | None = None) -> str:
        """Check device connectivity.

        Multi-pair mode when hosts list has >1 entry (device mapping list).
        Single mode for direct host+udid queries.
        """
        if hosts and len(hosts) > 1:
            return self._execute_multi(hosts, udids or [])

        if not host:
            return ":warning: No host IP found. Try: `@infra-bot 10.151.2.22,S499NNSGU8GUW8O7 check if connected`"
        if not udid:
            return ":warning: No device UDID/serial found. Try: `@infra-bot 10.151.2.22,S499NNSGU8GUW8O7 check if connected`"

        icon, status = _check_single(host, udid)
        logger.info("device_check host=%s udid=%s status=%s", host, udid, status)
        return f"{icon} *Device `{udid}`* on `{host}` — {status}"

    def _execute_multi(self, hosts: list, udids: list) -> str:
        """Check connectivity for multiple host,udid pairs."""
        pairs = list(zip(hosts, udids))[:50]
        lines = [":mag: *Device connectivity check*\n"]
        ok = fail = 0

        for host_ip, serial in pairs:
            if not serial:
                lines.append(f":warning: `{host_ip}` — no UDID, skipped")
                continue
            icon, status = _check_single(host_ip, serial)
            lines.append(f"{icon} `{host_ip}` / `{serial}` — {status}")
            if icon == ":white_check_mark:":
                ok += 1
            else:
                fail += 1

        lines.append(f"\n*Summary:* {ok} OK, {fail} failed out of {len(pairs)} pairs")
        logger.info("device_check multi: %d pairs, %d ok, %d fail", len(pairs), ok, fail)
        return "\n".join(lines)
