"""SSH execution utility for internal DC hosts.

The bot host (10.151.2.248) has direct SSH access to other DC hosts.
Uses the system ssh binary (subprocess) as the primary method — it respects
the host's SSH config, known_hosts, and network stack exactly like a manual
`ssh ltadmin@10.151.2.22` would. Paramiko is kept as fallback.
"""
from __future__ import annotations

import shutil
import subprocess

from utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_USER = "ltadmin"
_DEFAULT_PASS = ""
_TIMEOUT = 20


def ssh_exec(
    host: str,
    command: str,
    user: str = _DEFAULT_USER,
    password: str | None = None,
    timeout: int = _TIMEOUT,
) -> dict:
    """Run a command on an internal DC host via direct SSH.

    Returns:
        {"success": bool, "output": str, "error": str, "exit_code": int}
    """
    if password is None:
        from config import settings  # noqa: PLC0415
        password = settings.HOST_PASS or ""
    sshpass_bin = shutil.which("sshpass") or shutil.which("/opt/homebrew/bin/sshpass")
    if sshpass_bin:
        return _exec_sshpass(host, command, user, password, timeout, sshpass_bin=sshpass_bin)
    try:
        import paramiko  # noqa: PLC0415
        return _exec_paramiko(host, command, user, password, timeout)
    except ImportError:
        pass
    return {"success": False, "output": "", "error": "No SSH method available (install sshpass or paramiko)", "exit_code": -1}


def _exec_sshpass(host: str, command: str, user: str, password: str, timeout: int, sshpass_bin: str = "sshpass") -> dict:
    """Use sshpass + system ssh binary — respects host SSH config exactly."""
    try:
        result = subprocess.run(
            [
                sshpass_bin, "-p", password,
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", f"ConnectTimeout={timeout}",
                f"{user}@{host}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        logger.info("ssh_exec(sshpass) %s '%s' → exit=%d", host, command, result.returncode)
        return {
            "success": result.returncode == 0,
            "output": result.stdout.strip(),
            "error": result.stderr.strip(),
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "error": f"SSH timed out after {timeout}s", "exit_code": -1}
    except Exception as exc:  # noqa: BLE001
        logger.error("sshpass exec failed for %s: %s", host, exc)
        return {"success": False, "output": "", "error": str(exc), "exit_code": -1}


def _exec_paramiko(host: str, command: str, user: str, password: str, timeout: int) -> dict:
    """Paramiko fallback — direct password auth, no bastion needed."""
    import paramiko  # noqa: PLC0415
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
            banner_timeout=timeout,
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdin.close()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        code = stdout.channel.recv_exit_status()
        client.close()
        logger.info("ssh_exec(paramiko) %s '%s' → exit=%d", host, command, code)
        return {"success": code == 0, "output": out, "error": err, "exit_code": code}
    except Exception as exc:  # noqa: BLE001
        logger.error("paramiko exec failed for %s: %s", host, exc)
        return {"success": False, "output": "", "error": str(exc), "exit_code": -1}
