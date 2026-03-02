"""SSH action: execute commands on remote hosts via a bastion.

Only an explicit allowlist of commands is permitted.
"""
from config import settings
from .base_action import BaseAction

ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "reboot",
    "sudo reboot",
    "sudo /sbin/reboot",
    "uptime",
    "hostname",
    "uname -a",
    "df -h",
    "free -h",
    "ps aux",
    "systemctl status",
    "adb devices",
    "adb kill-server",
    "adb start-server",
    "uptime && df -h && free -h",
})


class SSHAction(BaseAction):
    action_type = "ssh_reboot"

    def execute(self) -> dict:
        try:
            import paramiko  # noqa: PLC0415
        except ImportError:
            return {
                "success": False,
                "message": "paramiko not installed. Run: pip install paramiko",
                "details": {},
            }

        target_host = self.params.get("host", "")
        command = self.params.get("command", "uptime")

        if not target_host:
            return {"success": False, "message": "No target host specified", "details": {}}

        normalized = command.strip().lower()
        if not any(
            normalized == a.lower() or normalized.startswith(a.lower())
            for a in ALLOWED_COMMANDS
        ):
            return {
                "success": False,
                "message": f"Command `{command}` is not in the SSH allowlist.",
                "details": {"allowed": sorted(ALLOWED_COMMANDS)},
            }

        if not settings.BASTION_HOST:
            return {
                "success": False,
                "message": "Bastion host not configured (BASTION_HOST env var missing)",
                "details": {},
            }

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=settings.BASTION_HOST,
                username=settings.BASTION_USER,
                key_filename=settings.BASTION_KEY_PATH,
                timeout=30,
            )
            transport = client.get_transport()
            channel = transport.open_channel(
                "direct-tcpip", (target_host, 22), (settings.BASTION_HOST, 22)
            )
            target = paramiko.SSHClient()
            target.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            target.connect(
                hostname=target_host,
                username=settings.BASTION_USER,
                key_filename=settings.BASTION_KEY_PATH,
                sock=channel,
                timeout=30,
            )
            _stdin, stdout, stderr = target.exec_command(command, timeout=60)
            output = stdout.read().decode(errors="replace").strip()
            error = stderr.read().decode(errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            target.close()
            client.close()
            return {
                "success": exit_code == 0,
                "message": f"Command `{command}` on `{target_host}`: exit={exit_code}",
                "details": {
                    "output": output[:500],
                    "error": error[:200],
                    "exit_code": exit_code,
                    "host": target_host,
                },
            }
        except Exception as exc:  # noqa: BLE001
            self.logger.error("SSH failed for %s: %s", target_host, exc)
            return {
                "success": False,
                "message": f"SSH connection failed to `{target_host}`",
                "details": {"error_type": type(exc).__name__},
            }
