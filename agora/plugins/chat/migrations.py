"""SQL migration strings for the Chat plugin.

Tables:
    chat_channels: Channel metadata (id, name, topic, metadata_json, created_at).
    chat_messages: Messages within channels (id, channel_id, agent_id, parent_id,
        content_type, content, created_at).

Indexes:
    idx_messages_channel_time: Fast lookup of messages by channel and time.
"""

_MIGRATIONS: list[str] = [
    """\
CREATE TABLE IF NOT EXISTS chat_channels (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    topic TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
)""",
    """\
CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    parent_id TEXT,
    content_type TEXT DEFAULT 'text',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES chat_channels(id)
)""",
    """\
CREATE INDEX IF NOT EXISTS idx_messages_channel_time
    ON chat_messages(channel_id, created_at)""",
]


def get_migrations() -> list[str]:
    """Return the ordered list of SQL migration strings.

    Returns:
        The list of SQL statements for the chat plugin schema.

    """
    return list(_MIGRATIONS)
