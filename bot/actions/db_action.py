"""DB action: read-only MySQL queries via optional SSH tunnel.

SAFETY: raises PermissionError on any non-SELECT statement.

If DB_TUNNEL_HOST is set in config, the action opens an SSH tunnel through
that bastion (using sshtunnel + paramiko — pure Python, no sshpass needed)
before connecting.  The tunnel is torn down after each query so the bot
holds no persistent connections.
"""
from __future__ import annotations

import re

from config import settings
from .base_action import BaseAction
from utils.logger import get_logger

logger = get_logger(__name__)

WRITE_PATTERN = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE|GRANT|REVOKE)\b',
    re.IGNORECASE,
)


class DBAction(BaseAction):
    action_type = "db_query"

    def execute(self) -> dict:
        query = self.params.get("query", "").strip()

        if not query:
            return {"success": False, "message": "No query specified", "details": {}}

        # SAFETY CHECK 1: detect write keywords
        match = WRITE_PATTERN.search(query)
        if match:
            raise PermissionError(
                f"Blocked non-SELECT query. Detected keyword: '{match.group(0).upper()}'. "
                "DBAction only allows read-only SELECT queries."
            )

        # SAFETY CHECK 2: must begin with SELECT
        if not query.upper().lstrip().startswith("SELECT"):
            raise PermissionError(
                "DBAction only allows queries beginning with SELECT."
            )

        if not all([settings.DB_HOST, settings.DB_USER, settings.DB_PASSWORD]):
            return {
                "success": False,
                "message": ":lock: Database not configured — `DB_HOST`, `DB_USER`, `DB_PASSWORD` missing in bot config",
                "details": {},
            }

        try:
            rows = self._run_query(query)
            return {
                "success": True,
                "message": f"Query returned {len(rows)} row(s)",
                "details": {
                    "row_count": len(rows),
                    "rows": rows,
                    "query_preview": query[:200],
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("DBAction query failed: %s", exc)
            return {
                "success": False,
                "message": f":x: Database query failed: {type(exc).__name__}: {exc}",
                "details": {"error_type": type(exc).__name__},
            }

    def _run_query(self, query: str) -> list[dict]:
        """Execute query, opening an SSH tunnel first if DB_TUNNEL_HOST is configured."""
        import pymysql  # noqa: PLC0415
        import pymysql.cursors  # noqa: PLC0415

        tunnel_host = settings.DB_TUNNEL_HOST

        if tunnel_host:
            return self._run_via_tunnel(query, pymysql)
        else:
            return self._run_direct(query, pymysql, settings.DB_HOST, settings.DB_PORT)

    def _run_via_tunnel(self, query: str, pymysql) -> list[dict]:
        """Open SSH tunnel then run the query."""
        import paramiko  # noqa: PLC0415 — patch before sshtunnel reads paramiko
        if not hasattr(paramiko, "DSsKey"):
            paramiko.DSsKey = paramiko.DSSKey  # sshtunnel compat with newer paramiko
        from sshtunnel import SSHTunnelForwarder  # noqa: PLC0415

        logger.info(
            "Opening SSH tunnel %s@%s:%d → %s:%d",
            settings.DB_TUNNEL_USER, settings.DB_TUNNEL_HOST, settings.DB_TUNNEL_PORT,
            settings.DB_HOST, settings.DB_PORT,
        )
        with SSHTunnelForwarder(
            (settings.DB_TUNNEL_HOST, settings.DB_TUNNEL_PORT),
            ssh_username=settings.DB_TUNNEL_USER,
            ssh_password=settings.DB_TUNNEL_PASS,
            remote_bind_address=(settings.DB_HOST, settings.DB_PORT),
        ) as tunnel:
            local_port = tunnel.local_bind_port
            logger.info("Tunnel up on local port %d", local_port)
            return self._run_direct(query, pymysql, "127.0.0.1", local_port)

    def _run_direct(self, query: str, pymysql, host: str, port: int) -> list[dict]:
        """Connect and execute query, returning up to 50 rows."""
        conn = pymysql.connect(
            host=host,
            port=port,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.DB_NAME,
            connect_timeout=15,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchmany(50)
        logger.info("Query returned %d row(s)", len(rows))
        return list(rows)
