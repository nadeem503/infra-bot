"""macOS launchd service actions — LRR, Resigner, IHM, Reconciler, LRP.

Host user: ltadmin
Plist dir: /Library/LaunchDaemons/
Services managed via: launchctl load/unload -w <plist>
"""
from __future__ import annotations

import re
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

# ── Service plist paths ───────────────────────────────────────────────────────

PLIST_BASE = "/Library/LaunchDaemons"

PLISTS = {
    "resigner":    f"{PLIST_BASE}/com.lambda.ios_resigner.plist",
    "patcher":     f"{PLIST_BASE}/com.lambda.patcher.plist",
    "ihm":         f"{PLIST_BASE}/com.lambda.ihm.plist",
    "reconciler":  f"{PLIST_BASE}/com.lambda.reconciler.plist",
    "lrp":         f"{PLIST_BASE}/com.lambda.lambda_remote_provider.plist",
    # LRR is per-UDID: com.lambda.lambda_remote_runner_<UDID>.plist
}

RESIGNER_HEALTH_PORT = 6789
KEYCHAIN_PATH = "/Users/ltadmin/Library/Keychains/login.keychain-db"


# ── Dry-run previews ──────────────────────────────────────────────────────────

class LRRRestartAction:
    """Restart Lambda Remote Runner for given UDID(s) on a macOS host."""

    action_type = "lrr_restart"

    def dry_run(self, params: dict) -> str:
        devices = params.get("devices", [])
        host = params.get("host", "<host_ip>")
        lines = [f"*Dry-run: LRR restart on `{host}`*"]
        if devices:
            for udid in devices:
                plist = f"{PLIST_BASE}/com.lambda.lambda_remote_runner_{udid}.plist"
                lines.append(f"1. `launchctl unload -w {plist}`")
                lines.append(f"2. `sleep 1`")
                lines.append(f"3. `launchctl load -w {plist}`")
        else:
            lines.append("1. `idevice_id -l`  ← discover connected UDIDs")
            lines.append("2. For each UDID: `launchctl unload/load com.lambda.lambda_remote_runner_<UDID>.plist`")
        lines.append(f"4. Verify: `launchctl list | grep com.lambda.lambda_remote_runner`")
        return "\n".join(lines)

    def execute(self, params: dict, ssh_exec) -> dict:
        devices = params.get("devices", [])
        results = []

        # If no UDIDs provided, discover from host
        if not devices:
            rc, out, err = ssh_exec("idevice_id -l")
            if rc == 0 and out.strip():
                devices = [u.strip() for u in out.strip().splitlines() if u.strip()]
                logger.info("Discovered UDIDs: %s", devices)
            else:
                return {"success": False, "error": "No UDIDs provided and idevice_id -l returned nothing"}

        for udid in devices:
            # Sanitize UDID — must be hex or alphanumeric
            if not re.match(r'^[0-9a-fA-F\-]{8,}$', udid):
                logger.warning("Skipping suspicious UDID: %s", udid)
                continue
            plist = f"{PLIST_BASE}/com.lambda.lambda_remote_runner_{udid}.plist"
            rc1, _, _ = ssh_exec(f"launchctl unload -w {plist}")
            ssh_exec("sleep 1")
            rc2, _, _ = ssh_exec(f"launchctl load -w {plist}")
            rc3, out3, _ = ssh_exec(f"launchctl list | grep com.lambda.lambda_remote_runner_{udid}")
            results.append({
                "udid": udid,
                "unload_rc": rc1,
                "load_rc": rc2,
                "running": rc3 == 0 and udid in out3,
            })

        success = all(r["running"] for r in results)
        return {"success": success, "results": results, "devices": devices}


class ResignerRestartAction:
    """Restart iOS Resigner service on a macOS host.

    Also unlocks keychain — required for resigner startup.
    Health check: curl http://HOST:6789/health → "OK"
    """

    action_type = "resigner_restart"

    def dry_run(self, params: dict) -> str:
        host = params.get("host", "<resigner_host>")
        return (
            f"*Dry-run: Resigner restart on `{host}`*\n"
            f"1. `security unlock-keychain -p <pwd> {KEYCHAIN_PATH}`\n"
            f"2. `launchctl unload -w {PLISTS['resigner']}`\n"
            f"3. `sleep 2`\n"
            f"4. `launchctl load -w {PLISTS['resigner']}`\n"
            f"5. `curl http://localhost:{RESIGNER_HEALTH_PORT}/health`  ← should return OK"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        keychain_password = params.get("keychain_password", "")

        steps = []

        # Unlock keychain if password provided
        if keychain_password:
            rc, _, _ = ssh_exec(
                f"security unlock-keychain -p {keychain_password} {KEYCHAIN_PATH}"
            )
            steps.append({"step": "unlock_keychain", "rc": rc})

        # Unload / load
        rc1, _, _ = ssh_exec(f"launchctl unload -w {PLISTS['resigner']}")
        ssh_exec("sleep 2")
        rc2, _, _ = ssh_exec(f"launchctl load -w {PLISTS['resigner']}")
        steps.append({"step": "reload_plist", "unload_rc": rc1, "load_rc": rc2})

        # Health check
        ssh_exec("sleep 3")
        rc3, out3, _ = ssh_exec(f"curl -s http://localhost:{RESIGNER_HEALTH_PORT}/health")
        healthy = rc3 == 0 and "OK" in out3
        steps.append({"step": "health_check", "rc": rc3, "response": out3.strip(), "healthy": healthy})

        return {"success": healthy, "steps": steps}


class IHMRestartAction:
    """Restart iOS Host Manager (IHM) on macOS."""

    action_type = "ihm_restart"

    def dry_run(self, params: dict) -> str:
        return (
            f"*Dry-run: IHM restart*\n"
            f"1. `launchctl unload -w {PLISTS['ihm']}`\n"
            f"2. `sleep 1`\n"
            f"3. `launchctl load -w {PLISTS['ihm']}`\n"
            f"4. `launchctl list | grep com.lambda.ihm`"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        rc1, _, _ = ssh_exec(f"launchctl unload -w {PLISTS['ihm']}")
        ssh_exec("sleep 1")
        rc2, _, _ = ssh_exec(f"launchctl load -w {PLISTS['ihm']}")
        ssh_exec("sleep 2")
        rc3, out3, _ = ssh_exec("launchctl list | grep com.lambda.ihm")
        running = rc3 == 0 and "ihm" in out3
        return {"success": running, "unload_rc": rc1, "load_rc": rc2, "launchctl_out": out3.strip()}


class ReconcilerRestartAction:
    """Restart Reconciler on macOS (launchctl) or Ubuntu (systemctl)."""

    action_type = "reconciler_restart"

    def dry_run(self, params: dict) -> str:
        host_type = params.get("host_type", "macos")
        if host_type == "ubuntu":
            return (
                "*Dry-run: Reconciler restart (Ubuntu)*\n"
                "1. `systemctl restart reconciler`\n"
                "2. `systemctl status reconciler`"
            )
        return (
            f"*Dry-run: Reconciler restart (macOS)*\n"
            f"1. `launchctl unload -w {PLISTS['reconciler']}`\n"
            f"2. `sleep 1`\n"
            f"3. `launchctl load -w {PLISTS['reconciler']}`\n"
            f"4. `launchctl list | grep com.lambda.reconciler`"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        host_type = params.get("host_type", "macos")
        if host_type == "ubuntu":
            rc1, _, _ = ssh_exec("systemctl restart reconciler")
            ssh_exec("sleep 2")
            rc2, out2, _ = ssh_exec("systemctl status reconciler --no-pager | head -5")
            running = rc2 == 0 and "running" in out2
            return {"success": running, "restart_rc": rc1, "status": out2.strip()}
        else:
            rc1, _, _ = ssh_exec(f"launchctl unload -w {PLISTS['reconciler']}")
            ssh_exec("sleep 1")
            rc2, _, _ = ssh_exec(f"launchctl load -w {PLISTS['reconciler']}")
            rc3, out3, _ = ssh_exec("launchctl list | grep com.lambda.reconciler")
            running = rc3 == 0 and "reconciler" in out3
            return {"success": running, "unload_rc": rc1, "load_rc": rc2}


class LRPRestartAction:
    """Restart Lambda Remote Provider on macOS or Ubuntu."""

    action_type = "lrp_restart"

    def dry_run(self, params: dict) -> str:
        host_type = params.get("host_type", "macos")
        if host_type == "ubuntu":
            return (
                "*Dry-run: LRP restart (Ubuntu)*\n"
                "1. `systemctl restart lambda_remote_provider`\n"
                "2. `systemctl status lambda_remote_provider`"
            )
        return (
            f"*Dry-run: LRP restart (macOS)*\n"
            f"1. `launchctl unload -w {PLISTS['lrp']}`\n"
            f"2. `sleep 1`\n"
            f"3. `launchctl load -w {PLISTS['lrp']}`\n"
            f"4. `launchctl list | grep com.lambda.lambda_remote_provider`"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        host_type = params.get("host_type", "macos")
        if host_type == "ubuntu":
            rc1, _, _ = ssh_exec("systemctl restart lambda_remote_provider")
            rc2, out2, _ = ssh_exec("systemctl status lambda_remote_provider --no-pager | head -5")
            running = rc2 == 0 and "running" in out2
            return {"success": running, "restart_rc": rc1, "status": out2.strip()}
        else:
            rc1, _, _ = ssh_exec(f"launchctl unload -w {PLISTS['lrp']}")
            ssh_exec("sleep 1")
            rc2, _, _ = ssh_exec(f"launchctl load -w {PLISTS['lrp']}")
            rc3, out3, _ = ssh_exec("launchctl list | grep lambda_remote_provider")
            running = rc3 == 0
            return {"success": running, "unload_rc": rc1, "load_rc": rc2}
