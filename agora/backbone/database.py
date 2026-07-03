"""Async SQLite database layer using apsw with WAL mode and migration tracking.

Single shared connection per server. WAL mode enables concurrent reads.
Migration tracking via SHA-256 hashes stored in backbone_config table.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import apsw
import apsw.bestpractice

logger = logging.getLogger(__name__)

# Apply apsw recommended best practices (WAL, busy timeout, FK, etc.)
apsw.bestpractice.apply(apsw.bestpractice.recommended)

_META_TABLE = """\
CREATE TABLE IF NOT EXISTS backbone_config (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
)"""


class Database:
    """Async SQLite database via apsw with WAL mode and migration support.

    Single shared connection per server. WAL mode enables concurrent reads.
    Migration tracking via SHA-256 hashes stored in backbone_config table.
    """

    def __init__(self, db_path: str = "agora.db") -> None:
        self._db_path = db_path
        self._conn: Any = None  # type: ignore[explicit-any]

    async def connect(self) -> None:
        """Open async apsw connection, enable WAL mode, create meta table."""
        self._conn = await apsw.Connection.as_async(self._db_path)
        await self._conn.pragma("journal_mode", "wal")
        await self._conn.pragma("busy_timeout", 5000)
        await self._conn.execute(_META_TABLE)
        logger.info("Database connected: %s", self._db_path)

    async def execute(  # type: ignore[explicit-any]
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        """Execute a parameterized query and return rows as dicts.

        Args:
            sql: SQL statement with ``?`` or ``:name`` placeholders.
            params: Bindings for the placeholders.

        Returns:
            List of dicts mapping column names to values.

        Raises:
            RuntimeError: If database is not connected.

        """
        self._ensure_connected()
        cursor = await self._conn.execute(sql, params)
        try:
            columns = [desc[0] for desc in cursor.getdescription()]
        except apsw.ExecutionCompleteError:
            return []
        raw_rows = await cursor.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in raw_rows]

    async def executemany(  # type: ignore[explicit-any]
        self,
        sql: str,
        params_list: list[dict[str, Any] | tuple[Any, ...]],
    ) -> None:
        """Execute the same statement with multiple parameter sets.

        Args:
            sql: SQL statement with placeholders.
            params_list: Sequence of binding sets.

        Raises:
            RuntimeError: If database is not connected.

        """
        self._ensure_connected()
        await self._conn.executemany(sql, params_list)

    async def execute_transaction(  # type: ignore[explicit-any]
        self,
        statements: list[tuple[str, dict[str, Any] | tuple[Any, ...]]],
    ) -> None:
        """Execute multiple statements in a single transaction.

        Args:
            statements: List of ``(sql, params)`` pairs.

        Raises:
            RuntimeError: If database is not connected.

        """
        self._ensure_connected()
        await self._conn.execute("BEGIN")
        try:
            for sql, params in statements:
                await self._conn.execute(sql, params)
            await self._conn.execute("COMMIT")
        except Exception:
            await self._conn.execute("ROLLBACK")
            raise

    async def run_migrations(
        self,
        plugin_name: str,
        migrations: list[str],
    ) -> list[str]:
        """Run pending migrations for a plugin.

        Returns list of applied hashes. Each migration is identified by SHA-256
        hash of its SQL content. Already-applied migrations are skipped. All
        pending migrations run in a single transaction — if one fails, none are
        applied.

        Args:
            plugin_name: Namespace prefix for migration keys.
            migrations: Ordered list of SQL migration strings.

        Returns:
            List of SHA-256 hashes (first 16 hex chars) for applied migrations.

        """
        self._ensure_connected()
        hashes: list[str] = []
        pending: list[tuple[str, str]] = []

        for sql in migrations:
            migration_hash = hashlib.sha256(sql.encode()).hexdigest()[:16]
            key = f"migration:{plugin_name}:{migration_hash}"
            cursor = await self._conn.execute(
                "SELECT value_json FROM backbone_config WHERE key = ?",
                (key,),
            )
            rows = await cursor.fetchall()
            if not rows:
                pending.append((migration_hash, sql))

        if pending:
            stmts: list[tuple[str, dict[str, Any] | tuple[Any, ...]]] = []  # type: ignore[explicit-any]
            for migration_hash, sql in pending:
                stmts.append((sql, ()))
                key = f"migration:{plugin_name}:{migration_hash}"
                stmts.append((
                    "INSERT INTO backbone_config (key, value_json) VALUES (?, ?)",
                    (key, "applied"),
                ))
                hashes.append(migration_hash)
            await self.execute_transaction(stmts)
            logger.info(
                "Applied %d migrations for %s",
                len(hashes),
                plugin_name,
            )

        return hashes

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            await self._conn.aclose()
            self._conn = None
            logger.info("Database closed: %s", self._db_path)

    def _ensure_connected(self) -> None:
        """Raise RuntimeError if not connected."""
        if self._conn is None:
            msg = "Database not connected. Call connect() first."
            raise RuntimeError(msg)
