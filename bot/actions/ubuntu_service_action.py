"""Ubuntu systemd service actions — RMDM, RDTSA, Android container restart.

Host user: ltadmin
Services managed via systemctl
Android devices run in Docker containers: adbd_<UDID>
"""
from __future__ import annotations

import re

from bot.actions.base_action import BaseAction
from utils.ssh_exec import ssh_exec as _ssh_exec
from utils.logger import get_logger

logger = get_logger(__name__)


def _run(host: str, cmd: str) -> tuple[int, str, str]:
    r = _ssh_exec(host, cmd)
    return r["exit_code"], r["output"], r["error"]


class RMDMRestartAction(BaseAction):
    """Restart RMDM (Real Device Docker Manager) on Ubuntu host."""

    action_type = "rmdm_restart"

    def dry_run(self) -> str:
        host = self.params.get("host", "<host>")
        return (
            f"*Dry-run: RMDM restart on `{host}`*\n"
            "1. `systemctl daemon-reload`\n"
            "2. `systemctl restart rmdm`\n"
            "3. `systemctl status rmdm --no-pager`\n"
            "4. `tail -20 /home/ltadmin/Documents/mobile-docker-manager/rmdm.log`"
        )

    def execute(self) -> dict:
        host = self.params.get("host", "")
        _run(host, "systemctl daemon-reload")
        rc1, _, _ = _run(host, "systemctl restart rmdm")
        _run(host, "sleep 3")
        rc2, out2, _ = _run(host, "systemctl status rmdm --no-pager | head -8")
        _, log3, _ = _run(
            host,
            "tail -10 /home/ltadmin/Documents/mobile-docker-manager/rmdm.log 2>/dev/null || echo 'no log'",
        )
        running = "running" in out2
        return {
            "success": running,
            "message": f"RMDM {'running' if running else 'not running'} on `{host}`",
            "restart_rc": rc1,
            "details": {"status": out2.strip(), "recent_log": log3.strip()},
        }


class RDTSARestartAction(BaseAction):
    """Restart RDTSA (Real Device Traffic Service Automation) on Ubuntu host."""

    action_type = "rdtsa_restart"

    def dry_run(self) -> str:
        host = self.params.get("host", "<host>")
        return (
            f"*Dry-run: RDTSA restart on `{host}`*\n"
            "1. `systemctl restart rdtsa`\n"
            "2. `systemctl status rdtsa --no-pager`\n"
            "3. `tail -20 /var/log/rdtsa.log`"
        )

    def execute(self) -> dict:
        host = self.params.get("host", "")
        rc1, _, _ = _run(host, "systemctl restart rdtsa")
        _run(host, "sleep 2")
        rc2, out2, _ = _run(host, "systemctl status rdtsa --no-pager | head -8")
        _, log3, _ = _run(host, "tail -10 /var/log/rdtsa.log 2>/dev/null || echo 'no log'")
        running = "running" in out2
        return {
            "success": running,
            "message": f"RDTSA {'running' if running else 'not running'} on `{host}`",
            "restart_rc": rc1,
            "details": {"status": out2.strip(), "recent_log": log3.strip()},
        }


class AndroidContainerRestartAction(BaseAction):
    """Restart Android ADB Docker container for a given UDID.

    Container name: adbd_<UDID>
    Tries adb reboot first; falls back to docker restart if container is stopped.
    """

    action_type = "android_container_restart"

    def dry_run(self) -> str:
        udid = self.params.get("udid", "") or (self.params.get("devices") or ["<UDID>"])[0]
        host = self.params.get("host", "<host>")
        return (
            f"*Dry-run: Android container restart for `{udid}` on `{host}`*\n"
            f"1. `docker exec -t adbd_{udid} adb -s {udid} reboot`\n"
            f"2. Wait ~70s for device to come back online\n"
            f"3. Check: `docker exec -t adbd_{udid} adb devices | grep device | wc -l`\n"
            f"   OR: `docker restart adbd_{udid}` (if container stopped)"
        )

    def execute(self) -> dict:
        host = self.params.get("host", "")
        udid = self.params.get("udid", "")
        devices = [udid] if udid else (self.params.get("devices") or [])
        if not devices:
            return {"success": False, "message": "No UDID provided", "details": {}}

        results = []
        for u in devices:
            if not re.match(r'^[0-9a-fA-F\-]{8,}$', u):
                logger.warning("Skipping suspicious UDID: %s", u)
                continue
            container = f"adbd_{u}"

            rc0, out0, _ = _run(host, f"docker ps --filter name={container} --format '{{{{.Names}}}}'")
            if container not in out0:
                rc_r, _, _ = _run(host, f"docker restart {container}")
                results.append({"udid": u, "method": "docker_restart", "rc": rc_r, "success": rc_r == 0})
                continue

            rc1, _, _ = _run(host, f"docker exec -t {container} adb -s {u} reboot")
            online = False
            for attempt in range(7):
                _run(host, "sleep 20")
                rc2, out2, _ = _run(host, f"docker exec -t {container} adb devices | grep {u} | grep -c device")
                if rc2 == 0 and out2.strip() == "1":
                    online = True
                    logger.info("Device %s back online after %d polls", u, attempt + 1)
                    break

            results.append({"udid": u, "method": "adb_reboot", "reboot_rc": rc1, "came_online": online, "success": online})

        success = all(r.get("success") for r in results)
        return {
            "success": success,
            "message": f"Container restart {'succeeded' if success else 'failed'} on `{host}`",
            "results": results,
            "details": {},
        }


class AllServicesStatusAction(BaseAction):
    """Check status of all key services on a host (macOS or Ubuntu)."""

    action_type = "host_service_status"

    def dry_run(self) -> str:
        host_type = self.params.get("host_type", "macos")
        host = self.params.get("host", "<host>")
        if host_type == "ubuntu":
            return (
                f"*Dry-run: All service status (Ubuntu) on `{host}`*\n"
                "1. `systemctl list-units --type=service | grep -E 'rmdm|rdtsa|lambda|reconciler'`\n"
                "2. `/usr/bin/go-adb listdevices | jq -r '.devicelist[].SerialNumber'`"
            )
        return (
            f"*Dry-run: All service status (macOS) on `{host}`*\n"
            "1. `launchctl list | grep com.lambda`\n"
            "2. `idevice_id -l`\n"
            "3. `curl -s http://localhost:6789/health`  ← resigner"
        )

    def execute(self) -> dict:
        host = self.params.get("host", "")
        host_type = self.params.get("host_type", "macos")
        if host_type == "ubuntu":
            _, svc_out, _ = _run(
                host,
                "systemctl list-units --type=service --no-pager | grep -E 'rmdm|rdtsa|lambda|reconciler'",
            )
            _, dev_out, _ = _run(
                host,
                "/usr/bin/go-adb listdevices 2>/dev/null | jq -r '.devicelist[].SerialNumber' 2>/dev/null || adb devices",
            )
            return {
                "success": True,
                "message": f"Service status retrieved for `{host}`",
                "details": {"services": svc_out.strip(), "devices": dev_out.strip()},
            }
        _, svc_out, _ = _run(host, "launchctl list | grep com.lambda")
        _, dev_out, _ = _run(host, "/opt/homebrew/bin/idevice_id -l 2>/dev/null || echo 'not found'")
        _, health_out, _ = _run(host, "curl -s http://localhost:6789/health 2>/dev/null || echo 'no response'")
        return {
            "success": True,
            "message": f"Service status retrieved for `{host}`",
            "details": {
                "services": svc_out.strip(),
                "devices": dev_out.strip(),
                "resigner_health": health_out.strip(),
            },
        }
