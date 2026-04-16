"""Claude MCP + Skills installer.

Runs at bot startup to ensure ltadmin's Claude CLI has:
- Slack + GitHub MCPs (merged into ~/.claude.json)
- mysql MCP (in ~/.claude/settings.json)
- disposed-device and device-migration skills (in ~/.claude/skills/)

This is idempotent — safe to call on every restart.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

_CONFIG_DIR = Path(__file__).parent
_HOME = Path.home()
_CLAUDE_DIR = _HOME / ".claude"
_CLAUDE_JSON = _HOME / ".claude.json"
_SETTINGS_JSON = _CLAUDE_DIR / "settings.json"
_SKILLS_DIR = _CLAUDE_DIR / "skills"
_MYSQL_MCP_SH = _CLAUDE_DIR / "mysql-mcp.sh"


def _substitute_env_vars(obj: dict) -> dict:
    """Recursively replace '${VAR_NAME}' placeholders with os.environ values.

    Tokens that are missing from env (or empty) are left as-is so the MCP
    server entry is still written — operator can fix the .env later.
    """
    import re
    if isinstance(obj, dict):
        return {k: _substitute_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env_vars(v) for v in obj]
    if isinstance(obj, str):
        def _repl(m: re.Match) -> str:
            val = os.environ.get(m.group(1), "")
            return val if val else m.group(0)   # keep placeholder if missing
        return re.sub(r"\$\{([^}]+)\}", _repl, obj)
    return obj


def _merge_claude_json() -> None:
    """Merge Slack + GitHub mcpServers into ~/.claude.json.

    The mcpservers.json template uses ${VAR} placeholders for secrets;
    actual values come from the bot's .env (SLACK_MCP_XOXC_TOKEN, etc.).
    """
    patch_file = _CONFIG_DIR / "mcpservers.json"
    if not patch_file.exists():
        return

    with open(patch_file) as f:
        new_servers: dict = _substitute_env_vars(json.load(f))

    # Skip if all token values are still placeholders (env not set yet)
    slack_cfg = new_servers.get("slack", {})
    slack_xoxc = slack_cfg.get("env", {}).get("SLACK_MCP_XOXC_TOKEN", "")
    if slack_xoxc.startswith("${"):
        logger.warning(
            "Claude config: SLACK_MCP_XOXC_TOKEN not set in .env — "
            "Slack MCP will not be configured. Add it to .env and restart."
        )
        new_servers.pop("slack", None)

    github_cfg = new_servers.get("github", {})
    github_pat = github_cfg.get("env", {}).get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if github_pat.startswith("${"):
        # Fallback: use GITHUB_TOKEN from bot config (already used for GH Actions)
        fallback_pat = os.environ.get("GITHUB_TOKEN", "")
        if fallback_pat:
            new_servers["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] = fallback_pat
        else:
            logger.warning(
                "Claude config: GITHUB_PERSONAL_ACCESS_TOKEN not set — "
                "GitHub MCP will not be configured. Add it to .env and restart."
            )
            new_servers.pop("github", None)

    if not new_servers:
        return

    existing: dict = {}
    if _CLAUDE_JSON.exists():
        try:
            with open(_CLAUDE_JSON) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing.setdefault("mcpServers", {})

    changed = False
    for name, cfg in new_servers.items():
        if existing["mcpServers"].get(name) != cfg:
            existing["mcpServers"][name] = cfg
            changed = True
            logger.info("Claude config: merged MCP '%s' into ~/.claude.json", name)

    if changed:
        with open(_CLAUDE_JSON, "w") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")


def _ensure_settings_json() -> None:
    """Ensure mysql MCP is in ~/.claude/settings.json."""
    _CLAUDE_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if _SETTINGS_JSON.exists():
        try:
            with open(_SETTINGS_JSON) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    mysql_entry = {"command": str(_MYSQL_MCP_SH)}
    if existing.get("mcpServers", {}).get("mysql") == mysql_entry:
        return  # already correct

    existing.setdefault("mcpServers", {})["mysql"] = mysql_entry
    with open(_SETTINGS_JSON, "w") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")
    logger.info("Claude config: mysql MCP set in ~/.claude/settings.json")


def _install_mysql_mcp_sh() -> None:
    """Copy mysql-mcp.sh into ~/.claude/ and make it executable."""
    src = _CONFIG_DIR / "mysql-mcp.sh"
    if not src.exists():
        return
    _CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    # Only overwrite if content differs
    if _MYSQL_MCP_SH.exists():
        if _MYSQL_MCP_SH.read_text() == src.read_text():
            return
    shutil.copy2(src, _MYSQL_MCP_SH)
    os.chmod(_MYSQL_MCP_SH, 0o755)
    logger.info("Claude config: installed ~/.claude/mysql-mcp.sh")


def _install_skills() -> None:
    """Copy skill SKILL.md files into ~/.claude/skills/."""
    src_skills = _CONFIG_DIR / "skills"
    if not src_skills.exists():
        return

    for skill_dir in src_skills.iterdir():
        if not skill_dir.is_dir():
            continue
        dest_dir = _SKILLS_DIR / skill_dir.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src_file in skill_dir.iterdir():
            dest_file = dest_dir / src_file.name
            if dest_file.exists() and dest_file.read_text() == src_file.read_text():
                continue
            shutil.copy2(src_file, dest_file)
            logger.info("Claude config: installed skill '%s/%s'", skill_dir.name, src_file.name)


def install_claude_config() -> None:
    """Entry point — call this at bot startup."""
    try:
        _install_mysql_mcp_sh()
        _ensure_settings_json()
        _merge_claude_json()
        _install_skills()
        logger.info("Claude config: MCP + skills sync complete")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude config installer failed (non-fatal): %s", exc)
