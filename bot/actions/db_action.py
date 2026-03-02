"""DB action: read-only MySQL queries.

SAFETY: raises PermissionError on any non-SELECT statement.
"""
import re

from config import settings
from .base_action import BaseAction

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
        if not query.upper().startswith("SELECT"):
            raise PermissionError(
                "DBAction only allows queries beginning with SELECT."
            )

        if not all([settings.DB_HOST, settings.DB_USER, settings.DB_PASSWORD]):
            return {
                "success": False,
                "message": "Database not configured (DB_HOST, DB_USER, DB_PASSWORD missing)",
                "details": {},
            }

        try:
            import pymysql  # noqa: PLC0415

            connection = pymysql.connect(
                host=settings.DB_HOST,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
                database=settings.DB_NAME,
                port=settings.DB_PORT,
                connect_timeout=10,
                cursorclass=pymysql.cursors.DictCursor,
            )
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(query)
                    rows = cursor.fetchmany(50)
            return {
                "success": True,
                "message": f"Query returned {len(rows)} row(s)",
                "details": {
                    "row_count": len(rows),
                    "rows": rows,
                    "query_preview": query[:100],
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"Database query failed: {type(exc).__name__}",
                "details": {"error_type": type(exc).__name__},
            }
