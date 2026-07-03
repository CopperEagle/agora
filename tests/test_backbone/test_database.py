"""Tests for the async SQLite Database layer with WAL mode and migration support."""

from __future__ import annotations

import asyncio
import hashlib
import pathlib
from collections.abc import AsyncGenerator

import apsw
import pytest

from agora.backbone.database import Database


@pytest.fixture
async def db(tmp_path: pathlib.Path) -> AsyncGenerator[Database, None]:
    """Provide a connected file-based Database, closed after each test."""
    path = tmp_path / "test.db"
    database = Database(str(path))
    await database.connect()
    yield database
    await database.close()


# ---------- Basic connectivity ----------


async def test_connect_memory() -> None:
    """Connecting to :memory: creates a usable database."""
    database = Database(":memory:")
    await database.connect()

    rows = await database.execute("SELECT 1 + 1 AS result")
    assert rows == [{"result": 2}]

    await database.close()


async def test_execute_parameterized() -> None:
    """Parameterized INSERT followed by SELECT returns correct data."""
    database = Database(":memory:")
    await database.connect()
    await database.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, val TEXT)")
    await database.execute("INSERT INTO kv (key, val) VALUES (?, ?)", ("name", "agora"))

    rows = await database.execute("SELECT val FROM kv WHERE key = ?", ("name",))
    assert rows == [{"val": "agora"}]

    await database.close()


async def test_executemany() -> None:
    """executemany inserts multiple rows in one call."""
    database = Database(":memory:")
    await database.connect()
    await database.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    await database.executemany(
        "INSERT INTO items (id, name) VALUES (?, ?)",
        [(1, "alpha"), (2, "beta"), (3, "gamma")],
    )

    rows = await database.execute("SELECT COUNT(*) AS cnt FROM items")
    assert rows == [{"cnt": 3}]

    await database.close()


# ---------- Pragma configuration ----------


async def test_wal_mode_enabled(db: Database) -> None:
    """After connect, journal_mode pragma returns 'wal'."""
    rows = await db.execute("PRAGMA journal_mode")
    assert rows[0]["journal_mode"] == "wal"


async def test_busy_timeout_set(db: Database) -> None:
    """After connect, busy_timeout pragma returns 5000."""
    rows = await db.execute("PRAGMA busy_timeout")
    assert rows[0]["timeout"] == 5000


async def test_backbone_config_table_created(db: Database) -> None:
    """The backbone_config table exists after connect."""
    rows = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='backbone_config'",
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "backbone_config"


# ---------- Migration system ----------


async def test_run_migrations_idempotent(db: Database) -> None:
    """Running the same migrations twice is a no-op the second time."""
    migrations = [
        "CREATE TABLE IF NOT EXISTS demo (id TEXT PRIMARY KEY)",
        "CREATE TABLE IF NOT EXISTS demo2 (id TEXT PRIMARY KEY)",
    ]

    applied_first = await db.run_migrations("test_plugin", migrations)
    assert len(applied_first) == 2

    applied_second = await db.run_migrations("test_plugin", migrations)
    assert len(applied_second) == 0


async def test_migration_tracking(db: Database) -> None:
    """After running a migration, its SHA-256 hash is stored in backbone_config."""
    sql = "CREATE TABLE IF NOT EXISTS tracked (id TEXT PRIMARY KEY)"
    await db.run_migrations("tracker", [sql])

    expected_hash = hashlib.sha256(sql.encode()).hexdigest()[:16]

    rows = await db.execute(
        "SELECT value_json FROM backbone_config WHERE key = ?",
        (f"migration:tracker:{expected_hash}",),
    )
    assert len(rows) == 1


async def test_migration_failure_rollback(db: Database) -> None:
    """If a migration fails, none of the batch migrations are applied."""
    migrations = [
        "CREATE TABLE IF NOT EXISTS should_exist (id TEXT PRIMARY KEY)",
        "THIS IS INVALID SQL THAT WILL FAIL",
    ]

    with pytest.raises(apsw.SQLError):
        await db.run_migrations("failing", migrations)

    # Neither table should exist since the transaction rolled back
    rows = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='should_exist'",
    )
    assert len(rows) == 0


# ---------- Lifecycle ----------


async def test_close() -> None:
    """Closing the database makes subsequent operations raise an error."""
    database = Database(":memory:")
    await database.connect()
    await database.close()

    with pytest.raises(RuntimeError, match="not connected"):
        await database.execute("SELECT 1")


# ---------- WAL concurrency ----------


async def test_concurrent_reads() -> None:
    """Two concurrent SELECT queries run without blocking each other."""
    database = Database(":memory:")
    await database.connect()
    await database.execute("CREATE TABLE counter (val INTEGER)")
    await database.execute("INSERT INTO counter (val) VALUES (42)")

    async def read_val() -> list[dict[str, int]]:
        return await database.execute("SELECT val FROM counter")

    results = await asyncio.gather(read_val(), read_val())
    assert results[0] == [{"val": 42}]
    assert results[1] == [{"val": 42}]

    await database.close()
