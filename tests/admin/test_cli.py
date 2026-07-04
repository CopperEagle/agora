"""Tests for the query functions in admin/cli.py.

Uses apsw.Connection(":memory:") directly — no backbone, no TTY.
"""

import apsw
import pytest

from admin.cli import (
    get_message_count,
    query_agents,
    query_channels,
    query_messages,
    verify_schema,
)

# ---------------------------------------------------------------------------
# Fixture: in-memory database with Agora schema
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> apsw.Connection:
    """Create an in-memory SQLite database with the Agora schema."""
    conn = apsw.Connection(":memory:")
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT,
            status TEXT DEFAULT 'offline',
            capabilities TEXT,
            current_task TEXT,
            last_heartbeat_at TEXT,
            registered_at TEXT DEFAULT (datetime('now')),
            manifest TEXT
        )
        """,
    )
    cur.execute(
        """
        CREATE TABLE chat_channels (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            topic TEXT
        )
        """,
    )
    cur.execute(
        """
        CREATE TABLE chat_messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL REFERENCES chat_channels(id),
            agent_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            parent_id TEXT,
            message_type TEXT DEFAULT 'message'
        )
        """,
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_channel_time
        ON chat_messages(channel_id, created_at)
        """,
    )
    return conn


# ---------------------------------------------------------------------------
# Test 1: verify_schema
# ---------------------------------------------------------------------------


def test_verify_schema(db: apsw.Connection) -> None:
    """verify_schema returns empty list when all tables exist."""
    missing = verify_schema(db)
    assert missing == []


# ---------------------------------------------------------------------------
# Test 2: query_agents
# ---------------------------------------------------------------------------


def test_query_agents(db: apsw.Connection) -> None:
    """query_agents returns agents with correct filtering and None handling."""
    cur = db.cursor()

    cur.execute(
        "INSERT INTO agents (id, name, role, capabilities, last_heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("a1", "worker-1", "worker", '["code"]', "2026-01-01T00:00:00"),
    )
    cur.execute(
        "INSERT INTO agents (id, name, role, capabilities, last_heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("a2", "reviewer-1", "reviewer", '["review"]', "2026-01-01T00:00:00"),
    )
    # agent with NULL last_heartbeat_at
    cur.execute(
        "INSERT INTO agents (id, name, role, capabilities, last_heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("a3", "scout-1", "scout", '["search"]', None),
    )

    # No filter — all 3 agents
    all_agents = query_agents(db)
    assert len(all_agents) == 3

    # Filter by role
    workers = query_agents(db, role_filter="worker")
    assert len(workers) == 1
    assert workers[0]["name"] == "worker-1"

    # NULL last_heartbeat_at rendered as dash
    scout = next(a for a in all_agents if a["name"] == "scout-1")
    assert scout["last_heartbeat_at"] == "-"

    # capabilities deserialized to list
    assert isinstance(workers[0]["capabilities"], list)


# ---------------------------------------------------------------------------
# Test 3: query_channels
# ---------------------------------------------------------------------------


def test_query_channels(db: apsw.Connection) -> None:
    """query_channels returns message counts and last activity timestamps."""
    cur = db.cursor()

    cur.execute(
        "INSERT INTO chat_channels (id, name) VALUES (?, ?)",
        ("ch1", "general"),
    )
    cur.execute(
        "INSERT INTO chat_channels (id, name) VALUES (?, ?)",
        ("ch2", "random"),
    )

    # 3 messages in general
    for i in range(3):
        cur.execute(
            "INSERT INTO chat_messages (id, channel_id, agent_id, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"m{i}", "ch1", "a1", f"msg {i}", f"2026-01-01T0{i}:00:00"),
        )

    # 1 message in random
    cur.execute(
        "INSERT INTO chat_messages (id, channel_id, agent_id, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m3", "ch2", "a1", "hello", "2026-01-01T00:00:00"),
    )

    # All channels
    channels = query_channels(db)
    assert len(channels) == 2

    general = next(c for c in channels if c["name"] == "general")
    random_ch = next(c for c in channels if c["name"] == "random")

    assert general["message_count"] == 3
    assert random_ch["message_count"] == 1
    assert general["last_activity_at"] == "2026-01-01T02:00:00"

    # Prefix filter
    gen_only = query_channels(db, name_prefix="gen")
    assert len(gen_only) == 1
    assert gen_only[0]["name"] == "general"


# ---------------------------------------------------------------------------
# Test 4: query_messages — order, limit, since
# ---------------------------------------------------------------------------


def test_query_messages(db: apsw.Connection) -> None:
    """query_messages supports ordering, limiting, and since filtering."""
    cur = db.cursor()

    cur.execute(
        "INSERT INTO chat_channels (id, name) VALUES (?, ?)",
        ("ch1", "test"),
    )

    # 5 messages with staggered timestamps and threading
    timestamps = [
        "2026-01-01T08:00:00",  # T-4h
        "2026-01-01T09:00:00",  # T-3h
        "2026-01-01T10:00:00",  # T-2h
        "2026-01-01T11:00:00",  # T-1h
        "2026-01-01T12:00:00",  # T-0h (now)
    ]
    parent_ids = [None, None, "m0", "m1", "m2"]  # some threaded
    for i, (ts, pid) in enumerate(zip(timestamps, parent_ids, strict=True)):
        if pid:
            cur.execute(
                "INSERT INTO chat_messages "
                "(id, channel_id, agent_id, content, created_at, parent_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"m{i}", "ch1", "a1", f"hello {i}", ts, pid),
            )
        else:
            cur.execute(
                "INSERT INTO chat_messages "
                "(id, channel_id, agent_id, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"m{i}", "ch1", "a1", f"hello {i}", ts),
            )

    # Order ascending — oldest first
    asc_msgs = query_messages(db, channel_name="test", order="asc")
    assert len(asc_msgs) == 5
    assert asc_msgs[0]["id"] == "m0"
    assert asc_msgs[4]["id"] == "m4"

    # Order descending — newest first
    desc_msgs = query_messages(db, channel_name="test", order="desc")
    assert len(desc_msgs) == 5
    assert desc_msgs[0]["id"] == "m4"
    assert desc_msgs[4]["id"] == "m0"

    # Limit
    limited = query_messages(db, channel_name="test", limit=2, order="desc")
    assert len(limited) == 2

    # Since filter (>= timestamp): T-3h = 09:00 → includes 09, 10, 11, 12 = 4 msgs
    since_msgs = query_messages(db, channel_name="test", since="2026-01-01T09:00:00")
    assert len(since_msgs) == 4

    # Verify threading: messages with parent_id have it in the dict
    threaded = [m for m in asc_msgs if m["parent_id"] is not None]
    assert len(threaded) == 3


# ---------------------------------------------------------------------------
# Test 5: query_messages — bad channel
# ---------------------------------------------------------------------------


def test_messages_bad_channel(db: apsw.Connection) -> None:
    """query_messages returns CHANNEL_NOT_FOUND error for nonexistent channel."""
    # Empty database — no channels at all
    result = query_messages(db, channel_name="nonexistent")
    assert isinstance(result, dict)
    assert result["error"] == "CHANNEL_NOT_FOUND"
    assert result["available_channels"] == []

    # Add a channel, then query nonexistent
    cur = db.cursor()
    cur.execute(
        "INSERT INTO chat_channels (id, name) VALUES (?, ?)",
        ("ch1", "general"),
    )
    result2 = query_messages(db, channel_name="nonexistent")
    assert result2["available_channels"] == ["general"]


# ---------------------------------------------------------------------------
# Test 6: get_message_count
# ---------------------------------------------------------------------------


def test_get_message_count(db: apsw.Connection) -> None:
    """get_message_count returns correct count or 0 for missing channel."""
    cur = db.cursor()

    cur.execute(
        "INSERT INTO chat_channels (id, name) VALUES (?, ?)",
        ("ch1", "general"),
    )
    for i in range(3):
        cur.execute(
            "INSERT INTO chat_messages (id, channel_id, agent_id, content) "
            "VALUES (?, ?, ?, ?)",
            (f"m{i}", "ch1", "a1", f"msg {i}"),
        )

    assert get_message_count(db, "ch1") == 3
    assert get_message_count(db, "nonexistent-id") == 0
