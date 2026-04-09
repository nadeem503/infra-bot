"""Device connectivity check action.

Host-type aware check flow:
  macOS (iOS devices):
    1. SSH uname -s → confirm Darwin
    2. idevice_id -l | grep <UDID>      — is device connected?
    3. tail LRR log for last error line  — lamda-remote-runner-<UDID>.log

  Ubuntu (Android devices):
    1. docker ps --filter name=adbd_<UDID>   — is container running?
    2. docker exec -i adbd_<UDID> adb -s <UDID> get-state  — device state

Host type detection order:
  1. SSH `uname -s` → Darwin=macOS, Linux=Ubuntu
  2. Fallback: UDID format — 8hex-dash-16hex or 40hex = iOS, else = Android

No approval workflow — read-only diagnostic, returns result directly.
"""
from __future__ import annotations

import re

from utils.logger import get_logger
from utils.ssh_exec import ssh_exec

logger = get_logger(__name__)

# iOS UDID patterns
_IOS_UDID_NEW = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{16}$')
_IOS_UDID_OLD = re.compile(r'^[0-9a-fA-F]{40}$')

_ANDROID_STATE_MAP = {
    "device":       (":white_check_mark:", "connected"),
    "offline":      (":warning:",          "OFFLINE"),
    "unauthorized": (":warning:",          "UNAUTHORIZED"),
    "bootloader":   (":warning:",          "in bootloader"),
    "recovery":     (":warning:",          "in recovery mode"),
}

LRR_LOG_DIR  = "/Users/ltadmin/Documents/LambdaRemoteRunner"
IHM_LOG      = "/Users/ltadmin/ios-host-manager/com.lambda.ihm.stdout"
LRP_MAC_LOG  = "/Users/ltadmin/Documents/LambdaRemoteProvider/lambda-remote-provider.log"
LRP_UBU_LOG  = "/home/ltadmin/Documents/LambdaRemoteProvider/lambda-remote-provider.log"
RECON_MAC_LOG = "/Users/ltadmin/reconciler/com.lambda.reconciler.stdout"
RECON_UBU_LOG = "/home/ltadmin/reconciler/runner.log"
RMDM_LOG     = "/home/ltadmin/rdtsa/logs/rdtsa.log"


def _is_ios_udid(udid: str) -> bool:
    return bool(_IOS_UDID_NEW.match(udid) or _IOS_UDID_OLD.match(udid))


def _detect_host_type(host: str) -> str:
    """Return 'macos', 'ubuntu', or 'unknown' by SSHing uname -s."""
    result = ssh_exec(host, "uname -s")
    if result["exit_code"] == 0:
        os_name = result["output"].strip().lower()
        if "darwin" in os_name:
            return "macos"
        if "linux" in os_name:
            return "ubuntu"
    return "unknown"


def _resolve_host_type(host: str, udid: str) -> str:
    """Primary: SSH uname -s. Fallback: UDID format."""
    host_type = _detect_host_type(host)
    if host_type != "unknown":
        return host_type
    # SSH failed — infer from UDID format
    logger.warning("host_type SSH detection failed for %s, inferring from UDID format", host)
    return "macos" if _is_ios_udid(udid) else "ubuntu"


# ── iOS / macOS check ─────────────────────────────────────────────────────────

def _tail_log(host: str, log_path: str, lines: int) -> str:
    """SSH tail a log file, return output text."""
    result = ssh_exec(host, f"tail -{lines} '{log_path}' 2>/dev/null || echo 'log not found'")
    return result["output"].strip()


_IDEVICE_ID = "/opt/homebrew/bin/idevice_id"   # full path — SSH non-interactive sessions skip /opt/homebrew/bin in PATH
_SLACK_LOG_MAX_CHARS = 2500                       # keep log output inside one Slack message


def _check_ios(host: str, udid: str, log_lines: int = 20) -> tuple[str, str]:
    """Check iOS device connectivity via idevice_id (full path) + LRR log."""
    # Step 1: is device listed by idevice_id? Use full path to avoid PATH issues in non-interactive SSH.
    connected = ssh_exec(host, f"{_IDEVICE_ID} -l 2>/dev/null | grep -c '{udid}'")
    if connected["exit_code"] == -1:
        return ":x:", f"SSH to `{host}` failed: {connected['error'][:100]}"

    count = connected["output"].strip()
    log_path = f"{LRR_LOG_DIR}/lamda-remote-runner-{udid}.log"
    log_output = _tail_log(host, log_path, log_lines)
    # Truncate to fit in one Slack message block
    if len(log_output) > _SLACK_LOG_MAX_CHARS:
        log_output = "..." + log_output[-_SLACK_LOG_MAX_CHARS:]

    if count != "1":
        # Check if LRR reports the device agent as healthy — if so this is likely a
        # momentary USB flicker (usbmuxd lost the device) rather than a real outage.
        lrr_healthy = any(s in log_output.lower() for s in (
            "ios-device-agent is healthy", "agent health notified", "200 ok",
        ))
        if lrr_healthy:
            return (
                ":warning:",
                f"not listed by idevice_id (possible USB flicker) — LRR agent is healthy\n"
                f"*LRR log (last {log_lines} lines):*\n```{log_output}```",
            )
        return ":x:", f"not connected (idevice_id)\n*LRR log (last {log_lines} lines):*\n```{log_output}```"

    # Device connected — show log, flag errors
    if any(w in log_output.lower() for w in ("error", "fail", "crash", "fatal", "exception")):
        return ":warning:", f"connected but LRR errors detected\n*LRR log (last {log_lines} lines):*\n```{log_output}```"

    return ":white_check_mark:", f"connected\n*LRR log (last {log_lines} lines):*\n```{log_output}```"


# ── Android / Ubuntu check ────────────────────────────────────────────────────

def _check_android(host: str, udid: str) -> tuple[str, str]:
    """Check Android device connectivity via Docker + ADB."""
    # Step 1: is the container running?
    ps = ssh_exec(host, f"docker ps --filter name=adbd_{udid} --format '{{{{.Status}}}}'")
    if ps["exit_code"] == -1:
        return ":x:", f"SSH to `{host}` failed: {ps['error'][:100]}"

    container_status = (ps["output"].strip().splitlines() or [""])[0]
    if not container_status:
        all_ps = ssh_exec(host, f"docker ps -a --filter name=adbd_{udid} --format '{{{{.Status}}}}'")
        stopped_status = all_ps["output"].strip()
        if stopped_status:
            return ":x:", "not connected (container stopped)"
        return ":x:", "not connected (container missing)"

    # Step 2: get device state from inside the container
    gs = ssh_exec(host, f"docker exec -i adbd_{udid} adb -s {udid} get-state 2>&1")
    raw = (gs["output"] or "").strip()

    if "error" in raw.lower() or "no devices" in raw.lower() or not raw:
        return ":x:", "not connected (device not responding)"

    state = raw.splitlines()[-1].strip()
    icon, label = _ANDROID_STATE_MAP.get(state, (":information_source:", state))
    return icon, label


# ── Unified entry point ───────────────────────────────────────────────────────

def _check_single(host: str, udid: str, log_lines: int = 50) -> tuple[str, str]:
    """Detect host type then run the correct check."""
    host_type = _resolve_host_type(host, udid)
    logger.info("device_check host=%s udid=%s host_type=%s log_lines=%d", host, udid, host_type, log_lines)

    if host_type == "macos":
        return _check_ios(host, udid, log_lines=log_lines)
    else:
        return _check_android(host, udid)


class DeviceCheckAction:
    """Runs connectivity check on a host and returns a Slack-ready reply.

    Automatically uses the correct check method based on host OS:
    - macOS  → idevice_id + LRR log (log_lines lines, default 50)
    - Ubuntu → Docker + ADB
    """

    def execute(
        self,
        host: str,
        udid: str = "",
        hosts: list | None = None,
        udids: list | None = None,
        log_lines: int = 50,
    ) -> str:
        """Check device connectivity.

        log_lines: number of log lines to tail (default 50, user can request more/less).
        Multi-pair mode when hosts list has >1 entry.
        Single mode for direct host+udid queries.
        """
        if hosts and len(hosts) > 1:
            return self._execute_multi(hosts, udids or [], log_lines=log_lines)

        if not host:
            return ":warning: No host IP found. Try: `@infra-bot 10.151.2.22,S499NNSGU8GUW8O7 check if connected`"
        if not udid:
            return ":warning: No device UDID/serial found. Try: `@infra-bot 10.151.2.22,S499NNSGU8GUW8O7 check if connected`"

        icon, status = _check_single(host, udid, log_lines=log_lines)
        logger.info("device_check host=%s udid=%s status=%s", host, udid, status)
        return f"{icon} *Device `{udid}`* on `{host}` — {status}"

    def _execute_multi(self, hosts: list, udids: list, log_lines: int = 50) -> str:
        """Check connectivity for multiple host,udid pairs."""
        pairs = list(zip(hosts, udids))[:50]
        lines = [":mag: *Device connectivity check*\n"]
        ok = fail = 0

        for host_ip, serial in pairs:
            if not serial:
                lines.append(f":warning: `{host_ip}` — no UDID, skipped")
                continue
            icon, status = _check_single(host_ip, serial, log_lines=log_lines)
            lines.append(f"{icon} `{host_ip}` / `{serial}` — {status}")
            if icon == ":white_check_mark:":
                ok += 1
            else:
                fail += 1

        lines.append(f"\n*Summary:* {ok} OK, {fail} failed out of {len(pairs)} pairs")
        logger.info("device_check multi: %d pairs, %d ok, %d fail", len(pairs), ok, fail)
        return "\n".join(lines)
