"""macOS launchd service actions — LRR, Resigner, IHM, Reconciler, LRP.

Host user: ltadmin
Plist dir: /Library/LaunchDaemons/
Services managed via: sudo launchctl unload/load -w <plist>

LRR is per-UDID: com.lambda.lambda_remote_runner_<UDID>.plist
  Reload sequence mirrors the production reload_remoterunner_plist.sh:
    1. sudo launchctl unload -w <plist>        (unload all first)
    2. sleep 2
    3. ideviceinfo -u <udid> → check iOS version ≥ 12.4
    4. sudo launchctl load -w <plist>          (load if compatible)
"""
from __future__ import annotations

import re

from bot.actions.base_action import BaseAction, ssh_run as _run
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

PLIST_BASE = "/Library/LaunchDaemons"
IDEVICEINFO = "/opt/homebrew/bin/ideviceinfo"
IDEVICE_ID  = "/opt/homebrew/bin/idevice_id"

PLISTS = {
    "resigner":   f"{PLIST_BASE}/com.lambda.ios_resigner.plist",
    "patcher":    f"{PLIST_BASE}/com.lambda.patcher.plist",
    "ihm":        f"{PLIST_BASE}/com.lambda.ihm.plist",
    "reconciler": f"{PLIST_BASE}/com.lambda.reconciler.plist",
    "lrp":        f"{PLIST_BASE}/com.lambda.lambda_remote_provider.plist",
}

RESIGNER_HEALTH_PORT = 6789
KEYCHAIN_PATH = "/Users/ltadmin/Library/Keychains/login.keychain-db"
_UDID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{16}$|^[0-9a-fA-F]{40}$')


# ── LRR (Lambda Remote Runner) ────────────────────────────────────────────────

class LRRRestartAction(BaseAction):
    """Reload Lambda Remote Runner plist(s) on a macOS host.

    Mirrors reload_remoterunner_plist.sh:
      1. Unload all UDIDs first (launchctl unload)
      2. sleep 2
      3. Check iOS version — skip if < 12.4 (CF not compatible)
      4. Load plist for compatible devices
    """

    action_type = "lrr_restart"

    def dry_run(self) -> str:
        host = self.params.get("host", "<host_ip>")
        udid = self.params.get("udid", "") or ""
        udids = [udid] if udid else self.params.get("devices", []) or []
        lines = [f"*Dry-run: LRR reload on `{host}`*"]
        if udids:
            for u in udids:
                plist = f"{PLIST_BASE}/com.lambda.lambda_remote_runner_{u}.plist"
                lines += [
                    f"1. `sudo launchctl unload -w {plist}`",
                    f"2. `sleep 2`",
                    f"3. Check iOS version via ideviceinfo (skip if < 12.4)",
                    f"4. `sudo launchctl load -w {plist}`",
                ]
        else:
            lines += [
                "1. `idevice_id -l` → discover UDIDs",
                "2. For each UDID: unload, sleep 2, version check, load",
            ]
        lines.append(f"5. `launchctl list | grep lambda_remote_runner`")
        return "\n".join(lines)

    def execute(self) -> dict:
        host = self.params.get("host", "")
        udid = self.params.get("udid", "") or ""

        # Guard: reject if host looks like a UDID instead of an IP address
        # This catches NLP misclassification where UDID gets set as host
        _IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
        if host and not _IP_RE.match(host):
            logger.error("LRRRestartAction: host '%s' is not a valid IP — aborting to prevent misfire", host)
            return {"success": False, "message": f"Invalid host `{host}` — expected an IP address, got what looks like a UDID. Please retry with the correct host IP."}

        # Gather UDIDs: explicit udid param, then devices list, then discover via idevice_id
        udids: list[str] = []
        if udid and _UDID_RE.match(udid):
            udids = [udid]
        else:
            raw = self.params.get("devices", []) or []
            udids = [u for u in raw if isinstance(u, str) and _UDID_RE.match(u)]

        if not udids:
            # Do NOT silently bulk-restart all devices — require explicit confirmation
            logger.warning("LRRRestartAction: no UDID specified for host %s — will discover all devices", host)
            rc, out, _ = _run(host, f"{IDEVICE_ID} -l 2>/dev/null")
            if rc == 0 and out.strip():
                udids = [u.strip() for u in out.strip().splitlines() if u.strip()]
                logger.info("LRRRestartAction discovered UDIDs: %s", udids)
            if not udids:
                return {"success": False, "message": "No UDIDs found — device not connected?"}

        results = []

        # Pass 1: unload all plists first
        for u in udids:
            plist = f"{PLIST_BASE}/com.lambda.lambda_remote_runner_{u}.plist"
            logger.info("LRR unload %s on %s", u, host)
            _run(host, f"sudo launchctl unload -w '{plist}'")

        _run(host, "sleep 2")

        # Pass 2: check version, then load
        for u in udids:
            plist = f"{PLIST_BASE}/com.lambda.lambda_remote_runner_{u}.plist"

            # iOS version check
            rc_v, ver_out, _ = _run(
                host,
                f"{IDEVICEINFO} -u {u} 2>/dev/null | grep ProductVersion | awk '{{print $2}}' | cut -c1-4",
            )
            version_str = ver_out.strip() if rc_v == 0 else ""
            try:
                version = float(version_str) if version_str else 99.0
            except ValueError:
                version = 99.0

            if version < 12.4:
                logger.info("LRR skip load for %s: iOS %.1f < 12.4 (CF not compatible)", u, version)
                results.append({
                    "udid": u, "loaded": False,
                    "note": f"iOS {version_str} < 12.4 — skipped (not CF compatible)",
                })
                continue

            logger.info("LRR load %s (iOS %s) on %s", u, version_str, host)
            rc_load, _, _ = _run(host, f"sudo launchctl load -w '{plist}'")
            rc_check, list_out, _ = _run(
                host, f"launchctl list | grep lambda_remote_runner_{u}"
            )
            loaded = rc_load == 0 or (rc_check == 0 and u in list_out)
            results.append({
                "udid": u, "loaded": loaded,
                "ios_version": version_str or "unknown",
                "load_rc": rc_load,
            })

        loaded_count = sum(1 for r in results if r.get("loaded"))
        total = len(results)
        success = loaded_count > 0
        msg = f"LRR reloaded {loaded_count}/{total} devices on `{host}`"
        if not success:
            msg = f"LRR reload failed for all devices on `{host}`"
        return {"success": success, "message": msg, "results": results, "details": {}}


# ── Resigner ──────────────────────────────────────────────────────────────────

class ResignerRestartAction(BaseAction):
    """Restart iOS Resigner service on a macOS host.

    Unlocks keychain before reload — required for resigner startup.
    Health: curl http://HOST:6789/health → "OK"
    """

    action_type = "resigner_restart"

    def dry_run(self) -> str:
        host = self.params.get("host", "<host>")
        return (
            f"*Dry-run: Resigner restart on `{host}`*\n"
            f"1. `security unlock-keychain -p <pwd> {KEYCHAIN_PATH}`\n"
            f"2. `sudo launchctl unload -w {PLISTS['resigner']}`\n"
            f"3. `sleep 2`\n"
            f"4. `sudo launchctl load -w {PLISTS['resigner']}`\n"
            f"5. `curl http://localhost:{RESIGNER_HEALTH_PORT}/health`  ← expect OK"
        )

    def execute(self) -> dict:
        from config import settings  # noqa: PLC0415
        host = self.params.get("host", "")
        passwd = settings.HOST_PASS or "lambdatest123!"

        steps = []
        # Pass password via stdin (not -p flag) to avoid exposure in ps aux
        rc0, _, _ = _run(host, f"echo {__import__('shlex').quote(passwd)} | security unlock-keychain -stdin '{KEYCHAIN_PATH}'")
        steps.append({"step": "unlock_keychain", "rc": rc0})

        rc1, _, _ = _run(host, f"sudo launchctl unload -w '{PLISTS['resigner']}'")
        _run(host, "sleep 2")
        rc2, _, _ = _run(host, f"sudo launchctl load -w '{PLISTS['resigner']}'")
        steps.append({"step": "reload_plist", "unload_rc": rc1, "load_rc": rc2})

        _run(host, "sleep 3")
        rc3, out3, _ = _run(host, f"curl -s http://localhost:{RESIGNER_HEALTH_PORT}/health")
        healthy = rc3 == 0 and "OK" in out3
        steps.append({"step": "health_check", "rc": rc3, "response": out3.strip(), "healthy": healthy})

        return {
            "success": healthy,
            "message": f"Resigner {'healthy' if healthy else 'unhealthy'} after restart on `{host}`",
            "steps": steps,
            "details": {},
        }


# ── IHM (iOS Host Manager) ────────────────────────────────────────────────────

class IHMRestartAction(BaseAction):
    """Restart iOS Host Manager (IHM) on macOS via launchctl."""

    action_type = "ihm_restart"

    def dry_run(self) -> str:
        host = self.params.get("host", "<host>")
        return (
            f"*Dry-run: IHM restart on `{host}`*\n"
            f"1. `sudo launchctl unload -w {PLISTS['ihm']}`\n"
            f"2. `sleep 2`\n"
            f"3. `sudo launchctl load -w {PLISTS['ihm']}`\n"
            f"4. `launchctl list | grep com.lambda.ihm`"
        )

    def execute(self) -> dict:
        host = self.params.get("host", "")
        rc1, _, _ = _run(host, f"sudo launchctl unload -w '{PLISTS['ihm']}'")
        _run(host, "sleep 2")
        rc2, _, _ = _run(host, f"sudo launchctl load -w '{PLISTS['ihm']}'")
        _run(host, "sleep 2")
        rc3, out3, _ = _run(host, "launchctl list | grep com.lambda.ihm")
        running = rc3 == 0 and "ihm" in out3
        return {
            "success": running,
            "message": f"IHM {'running' if running else 'not running'} after restart on `{host}`",
            "unload_rc": rc1, "load_rc": rc2,
            "details": {"launchctl_out": out3.strip()},
        }


# ── Reconciler ────────────────────────────────────────────────────────────────

class ReconcilerRestartAction(BaseAction):
    """Restart Reconciler on macOS (launchctl) or Ubuntu (systemctl)."""

    action_type = "reconciler_restart"

    def dry_run(self) -> str:
        host_type = self.params.get("host_type", "macos")
        host = self.params.get("host", "<host>")
        if host_type == "ubuntu":
            return (
                f"*Dry-run: Reconciler restart (Ubuntu) on `{host}`*\n"
                "1. `systemctl restart reconciler`\n"
                "2. `systemctl status reconciler`"
            )
        return (
            f"*Dry-run: Reconciler restart (macOS) on `{host}`*\n"
            f"1. `sudo launchctl unload -w {PLISTS['reconciler']}`\n"
            f"2. `sleep 2`\n"
            f"3. `sudo launchctl load -w {PLISTS['reconciler']}`\n"
            f"4. `launchctl list | grep com.lambda.reconciler`"
        )

    def execute(self) -> dict:
        host = self.params.get("host", "")
        host_type = self.params.get("host_type", "macos")
        if host_type == "ubuntu":
            rc1, _, _ = _run(host, "systemctl restart reconciler")
            _run(host, "sleep 2")
            rc2, out2, _ = _run(host, "systemctl status reconciler --no-pager | head -5")
            running = rc2 == 0 and "running" in out2
            return {
                "success": running,
                "message": f"Reconciler {'running' if running else 'not running'} on `{host}`",
                "restart_rc": rc1,
                "details": {"status": out2.strip()},
            }
        rc1, _, _ = _run(host, f"sudo launchctl unload -w '{PLISTS['reconciler']}'")
        _run(host, "sleep 2")
        rc2, _, _ = _run(host, f"sudo launchctl load -w '{PLISTS['reconciler']}'")
        rc3, out3, _ = _run(host, "launchctl list | grep com.lambda.reconciler")
        running = rc3 == 0 and "reconciler" in out3
        return {
            "success": running,
            "message": f"Reconciler {'running' if running else 'not running'} on `{host}`",
            "unload_rc": rc1, "load_rc": rc2,
            "details": {},
        }


# ── LRP (Lambda Remote Provider) ──────────────────────────────────────────────

class LRPRestartAction(BaseAction):
    """Restart Lambda Remote Provider on macOS or Ubuntu."""

    action_type = "lrp_restart"

    def dry_run(self) -> str:
        host_type = self.params.get("host_type", "macos")
        host = self.params.get("host", "<host>")
        if host_type == "ubuntu":
            return (
                f"*Dry-run: LRP restart (Ubuntu) on `{host}`*\n"
                "1. `systemctl restart lambda_remote_provider`\n"
                "2. `systemctl status lambda_remote_provider`"
            )
        return (
            f"*Dry-run: LRP restart (macOS) on `{host}`*\n"
            f"1. `sudo launchctl unload -w {PLISTS['lrp']}`\n"
            f"2. `sleep 2`\n"
            f"3. `sudo launchctl load -w {PLISTS['lrp']}`\n"
            f"4. `launchctl list | grep lambda_remote_provider`"
        )

    def execute(self) -> dict:
        host = self.params.get("host", "")
        host_type = self.params.get("host_type", "macos")
        if host_type == "ubuntu":
            rc1, _, _ = _run(host, "systemctl restart lambda_remote_provider")
            _run(host, "sleep 2")
            rc2, out2, _ = _run(host, "systemctl status lambda_remote_provider --no-pager | head -5")
            running = rc2 == 0 and "running" in out2
            return {
                "success": running,
                "message": f"LRP {'running' if running else 'not running'} on `{host}`",
                "restart_rc": rc1,
                "details": {"status": out2.strip()},
            }
        rc1, _, _ = _run(host, f"sudo launchctl unload -w '{PLISTS['lrp']}'")
        _run(host, "sleep 2")
        rc2, _, _ = _run(host, f"sudo launchctl load -w '{PLISTS['lrp']}'")
        rc3, out3, _ = _run(host, "launchctl list | grep lambda_remote_provider")
        running = rc3 == 0
        return {
            "success": running,
            "message": f"LRP {'running' if running else 'not running'} on `{host}`",
            "unload_rc": rc1, "load_rc": rc2,
            "details": {},
        }
