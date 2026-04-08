"""Ubuntu systemd service actions — RMDM, RDTSA, Android container restart.

Host user: ltadmin
Services managed via systemctl
Android devices run in Docker containers: adbd_<UDID>
"""
from __future__ import annotations

import re

from utils.logger import get_logger

logger = get_logger(__name__)


class RMDMRestartAction:
    """Restart RMDM (Real Device Docker Manager) on Ubuntu host.

    Binary: /home/ltadmin/Documents/mobile-docker-manager/rmdm
    Service: rmdm.service (systemd)
    Log: /home/ltadmin/Documents/mobile-docker-manager/rmdm.log
    """

    action_type = "rmdm_restart"

    def dry_run(self, params: dict) -> str:
        return (
            "*Dry-run: RMDM restart (Ubuntu)*\n"
            "1. `systemctl daemon-reload`\n"
            "2. `systemctl restart rmdm`\n"
            "3. `systemctl status rmdm --no-pager`\n"
            "4. `tail -20 /home/ltadmin/Documents/mobile-docker-manager/rmdm.log`"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        rc0, _, _ = ssh_exec("systemctl daemon-reload")
        rc1, _, _ = ssh_exec("systemctl restart rmdm")
        ssh_exec("sleep 3")
        rc2, out2, _ = ssh_exec("systemctl status rmdm --no-pager | head -8")
        rc3, log3, _ = ssh_exec(
            "tail -10 /home/ltadmin/Documents/mobile-docker-manager/rmdm.log 2>/dev/null || echo 'no log'"
        )
        running = "running" in out2
        return {
            "success": running,
            "daemon_reload_rc": rc0,
            "restart_rc": rc1,
            "status": out2.strip(),
            "recent_log": log3.strip(),
        }


class RDTSARestartAction:
    """Restart RDTSA (Real Device Traffic Service Automation) on Ubuntu host.

    Binary: /home/ltadmin/rdtsa/rdtsa
    Service: rdtsa.service (systemd)
    Log: /var/log/rdtsa.log
    """

    action_type = "rdtsa_restart"

    def dry_run(self, params: dict) -> str:
        return (
            "*Dry-run: RDTSA restart (Ubuntu)*\n"
            "1. `systemctl restart rdtsa`\n"
            "2. `systemctl status rdtsa --no-pager`\n"
            "3. `tail -20 /var/log/rdtsa.log`"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        rc1, _, _ = ssh_exec("systemctl restart rdtsa")
        ssh_exec("sleep 2")
        rc2, out2, _ = ssh_exec("systemctl status rdtsa --no-pager | head -8")
        rc3, log3, _ = ssh_exec("tail -10 /var/log/rdtsa.log 2>/dev/null || echo 'no log'")
        running = "running" in out2
        return {
            "success": running,
            "restart_rc": rc1,
            "status": out2.strip(),
            "recent_log": log3.strip(),
        }


class AndroidContainerRestartAction:
    """Restart Android ADB Docker container for a given UDID.

    Container name pattern: adbd_<UDID>
    Reboot command: docker exec -t adbd_<UDID> adb -s <UDID> reboot
    Wait for device to come back online (~70s, check every 20s for 7 attempts).
    """

    action_type = "android_container_restart"

    def dry_run(self, params: dict) -> str:
        devices = params.get("devices", ["<UDID>"])
        udid = devices[0] if devices else "<UDID>"
        return (
            f"*Dry-run: Android container restart for `{udid}`*\n"
            f"1. `docker exec -t adbd_{udid} adb -s {udid} reboot`\n"
            f"2. Wait ~70s for device to come back online\n"
            f"3. Check: `docker exec -t adbd_{udid} adb devices | grep device | wc -l`\n"
            f"   OR: `docker restart adbd_{udid}` (if reboot unreachable)"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        devices = params.get("devices", [])
        if not devices:
            return {"success": False, "error": "No UDID provided"}

        results = []
        for udid in devices:
            if not re.match(r'^[0-9a-fA-F\-]{8,}$', udid):
                logger.warning("Skipping suspicious UDID: %s", udid)
                continue

            container = f"adbd_{udid}"

            # Check if container is running first
            rc0, out0, _ = ssh_exec(f"docker ps --filter name={container} --format '{{{{.Names}}}}'")
            if container not in out0:
                # Container not running — restart it
                rc_r, _, _ = ssh_exec(f"docker restart {container}")
                results.append({
                    "udid": udid,
                    "method": "docker_restart",
                    "rc": rc_r,
                    "success": rc_r == 0,
                })
                continue

            # Container running — send reboot via ADB
            rc1, _, _ = ssh_exec(f"docker exec -t {container} adb -s {udid} reboot")

            # Wait and poll for device to come back (max 7 x 20s = 140s)
            online = False
            for attempt in range(7):
                ssh_exec("sleep 20")
                rc2, out2, _ = ssh_exec(
                    f"docker exec -t {container} adb devices | grep {udid} | grep -c device"
                )
                if rc2 == 0 and out2.strip() == "1":
                    online = True
                    logger.info("Device %s back online after %d checks", udid, attempt + 1)
                    break

            results.append({
                "udid": udid,
                "method": "adb_reboot",
                "reboot_rc": rc1,
                "came_online": online,
                "success": online,
            })

        success = all(r.get("success") for r in results)
        return {"success": success, "results": results}


class AllServicesStatusAction:
    """Check status of all key services on a host (macOS or Ubuntu)."""

    action_type = "host_service_status"

    def dry_run(self, params: dict) -> str:
        host_type = params.get("host_type", "macos")
        if host_type == "ubuntu":
            return (
                "*Dry-run: All service status (Ubuntu)*\n"
                "1. `systemctl list-units --type=service | grep -E 'rmdm|rdtsa|lambda|reconciler'`\n"
                "2. `/usr/bin/go-adb listdevices | jq -r '.devicelist[].SerialNumber'`"
            )
        return (
            "*Dry-run: All service status (macOS)*\n"
            "1. `launchctl list | grep com.lambda`\n"
            "2. `idevice_id -l`\n"
            "3. `curl -s http://localhost:6789/health`  ← resigner"
        )

    def execute(self, params: dict, ssh_exec) -> dict:
        host_type = params.get("host_type", "macos")
        if host_type == "ubuntu":
            _, svc_out, _ = ssh_exec(
                "systemctl list-units --type=service --no-pager | grep -E 'rmdm|rdtsa|lambda|reconciler'"
            )
            _, dev_out, _ = ssh_exec(
                "/usr/bin/go-adb listdevices 2>/dev/null | jq -r '.devicelist[].SerialNumber' 2>/dev/null || adb devices"
            )
            return {"success": True, "services": svc_out.strip(), "devices": dev_out.strip()}
        else:
            _, svc_out, _ = ssh_exec("launchctl list | grep com.lambda")
            _, dev_out, _ = ssh_exec("idevice_id -l 2>/dev/null || echo 'idevice_id not found'")
            _, health_out, _ = ssh_exec(f"curl -s http://localhost:{6789}/health 2>/dev/null || echo 'no response'")
            return {
                "success": True,
                "services": svc_out.strip(),
                "devices": dev_out.strip(),
                "resigner_health": health_out.strip(),
            }
