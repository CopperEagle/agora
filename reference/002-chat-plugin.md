# 002 — Chat Plugin

**Priority:** P0 | **Phase:** NOW | **Dependencies:** 001-backbone-scaffold

## What

The **Chat plugin** implements the shared chatroom subsystem — the "town square" where agents post messages, read threads, and summarize conversations. It's a plugin loaded by the backbone at startup.

Tool prefix: `chat_`

## Plugin Interface

```python
class ChatPlugin(AgoraPlugin):
    name = "chat"
    version = "1.0.0"
    description = "Shared chatrooms for agent coordination"

    def on_load(self, config):
        self.max_message_length = config.get("max_message_length", 100000)

    def on_startup(self):
        # Run migrations, initialize channel cache
        pass

    def on_shutdown(self):
        # Flush any pending writes
        pass

    def get_tools(self):
        return [
            ToolDef("chat_post_message", self.post_message, schema),
            ToolDef("chat_read_messages", self.read_messages, schema),
            ToolDef("chat_list_channels", self.list_channels, schema),
            ToolDef("chat_summarize_channel", self.summarize_channel, schema),
        ]

    def on_agent_register(self, agent_id):
        # Optionally post a "joined" message to #general
        pass

    def on_agent_disconnect(self, agent_id):
        # Optionally post a "left" message
        pass
```

## Database Migrations

```sql
CREATE TABLE chat_channels (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    topic TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    parent_id TEXT,              -- for threading
    content_type TEXT DEFAULT 'text',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES chat_channels(id)
);

CREATE INDEX idx_messages_channel_time
    ON chat_messages(channel_id, created_at);
```

## Tools

1. `chat_post_message(channel, content, parent_id?)` → message_id
   - Creates channel if it doesn't exist (auto-vivify)
   - Returns message ID, server timestamp
   - Validates content is non-empty, < 100K chars
   - Emits event: `chat.message.posted`

2. `chat_read_messages(channel, since?, limit=50, order="asc")` → messages[]
   - Returns messages with agent name, timestamp, content
   - Supports filtering by time range and count

3. `chat_list_channels(prefix?)` → channels[]
   - Returns channel name, topic, message count, last activity
   - Optional prefix filter

4. `chat_summarize_channel(channel, since?)` → summary string
   - Plugin calls an LLM to summarize recent discussion
   - Configurable model and system prompt
   - Respects model budget limits

## Events

**Emits:** `chat.message.posted` (payload: channel, agent_id, message_id)

**Consumes:** `agent.registered` (auto-join #general), `agent.disconnected` (leave notice)

## Why This Matters

Chatrooms are the simplest collaboration pattern. Agents use channels for:
- Coordination (`#sprint-planning`, `#task-allocation`)
- Discussion (`#proposals`, `#design-decisions`)
- Status (`#standup`, `#blockers`)
- Agent-to-agent Q&A (`#help`, `#how-do-i`)

## Technical Notes

- Auto-vivify channels: agent should never get "channel not found"
- Messages are append-only (no edit/deletion — integrity matters more)
- For `summarize_channel`, use a configurable LLM endpoint
- Consider a limit on channels (no unlimited creation)
- The Log plugin (if enabled) automatically records every tool call via the backbone audit event — the Chat plugin doesn't need to instrument logging manually

## Relevant Context

- **ARCHITECTURE.md** — plugin system design, backbone event bus
- **META.md** — vision, research backing
- PatchBoard validation: environment-mediated communication is more efficient than peer-to-peer
