"""Direct SSH execution utility for internal DC hosts.

Hosts on the 10.151.x.x subnet are reachable directly from the bot host
without a bastion. Uses paramiko with password auth (no key required).
"""
from __future__ import annotations

import subprocess
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_USER = "ltadmin"
_DEFAULT_PASS = "lambdatest123!"
_TIMEOUT = 20


def ssh_exec(
    host: str,
    command: str,
    user: str = _DEFAULT_USER,
    password: str = _DEFAULT_PASS,
    timeout: int = _TIMEOUT,
) -> dict:
    """Run a command on an internal host via direct SSH.

    Returns:
        {"success": bool, "output": str, "error": str, "exit_code": int}
    """
    try:
        import paramiko  # noqa: PLC0415
        return _exec_paramiko(host, command, user, password, timeout)
    except ImportError:
        logger.warning("paramiko not installed — falling back to subprocess expect")
        return _exec_subprocess(host, command, user, password, timeout)


def _exec_paramiko(host: str, command: str, user: str, password: str, timeout: int) -> dict:
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
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        code = stdout.channel.recv_exit_status()
        client.close()
        logger.debug("ssh_exec %s '%s' → exit=%d", host, command, code)
        return {"success": code == 0, "output": out, "error": err, "exit_code": code}
    except Exception as exc:  # noqa: BLE001
        logger.error("paramiko ssh_exec failed for %s: %s", host, exc)
        return {"success": False, "output": "", "error": str(exc), "exit_code": -1}


def _exec_subprocess(host: str, command: str, user: str, password: str, timeout: int) -> dict:
    """Fallback using sshpass if paramiko is unavailable."""
    try:
        result = subprocess.run(
            ["sshpass", "-p", password, "ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", f"ConnectTimeout={timeout}",
             f"{user}@{host}", command],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout.strip(),
            "error": result.stderr.strip(),
            "exit_code": result.returncode,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("subprocess ssh_exec failed for %s: %s", host, exc)
        return {"success": False, "output": "", "error": str(exc), "exit_code": -1}
