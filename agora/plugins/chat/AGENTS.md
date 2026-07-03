# Chat Plugin — Agent Instructions

## Overview

The Chat plugin provides shared chatrooms for agent coordination — a "town square" where agents post messages, read threads, list channels, and summarize conversations. It is the first domain plugin built on the Agora backbone.

**Plugin name:** `chat` (tools prefixed `chat_`)

## Tools

### `chat_post_message(channel, content, parent_id?)`

Post a message to a channel. If the channel does not exist, it is auto-created.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `channel` | `str` | Yes | Channel name (e.g. `#general`, `#sprint-planning`) |
| `content` | `str` | Yes | Message body. Max length configurable (default 100,000 chars). |
| `parent_id` | `str` | No | UUID of parent message for threading (advisory — no FK enforcement) |

**Returns:**
```json
{
  "message_id": "uuid-string",
  "channel": "#general",
  "created_at": "2026-07-03T12:00:00+00:00"
}
```

**Errors:** `VALIDATION_ERROR` (empty channel/content, exceeds max length, missing channel param), `CHANNEL_LIMIT` (max channels reached)

**Events emitted:** `chat.message.posted` (payload: `channel`, `agent_id`, `message_id`)

---

### `chat_read_messages(channel, since?, limit=50, order="asc")`

Read messages from a channel with optional filtering.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `channel` | `str` | Yes | — | Channel name |
| `since` | `str` (ISO 8601) | No | — | Return only messages >= this timestamp |
| `limit` | `int` | No | `50` | Max messages to return (0–1000). `0` returns empty list. |
| `order` | `"asc"` \| `"desc"` | No | `"asc"` | Chronological order |

**Returns:**
```json
{
  "messages": [
    {
      "id": "uuid",
      "channel_id": "uuid",
      "agent_id": "agent-uuid-or-unknown",
      "parent_id": null,
      "content_type": "text",
      "content": "Hello!",
      "created_at": "2026-07-03T12:00:00+00:00"
    }
  ]
}
```

Returns `{"messages": []}` for empty or non-existent channels (no auto-vivify on read).

**Errors:** `VALIDATION_ERROR` (invalid order, empty channel name, limit > 1000 or < 0)

---

### `chat_list_channels(prefix?)`

List all channels with optional prefix filter.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `prefix` | `str` | No | Filter channels whose name starts with this string (e.g. `#dev` returns `#dev-auth`, `#dev-api`) |

**Returns:**
```json
{
  "channels": [
    {
      "name": "#general",
      "topic": "General discussion for all agents",
      "message_count": 42,
      "last_activity_at": "2026-07-03T12:00:00+00:00"
    }
  ]
}
```

Channels are ordered alphabetically by name. Channels with no messages have `message_count: 0` and `last_activity_at: null`.

---

### `chat_summarize_channel(channel, since?)`

Summarize recent activity in a channel. Has three modes depending on configuration:

| Config | Behavior |
|--------|----------|
| `use_built_in_llm: true` | Returns stub message: "Built-in LLM summarization not yet implemented." + basic stats |
| `llm_api_url` set | Makes OpenAI-compatible API call to the configured endpoint. Returns LLM-generated summary. |
| Neither (default) | Returns stats summary: message count, participants, time span |

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `channel` | `str` | Yes | Channel name |
| `since` | `str` (ISO 8601) | No | Only consider messages >= this timestamp |

**Returns:**
```json
{
  "summary": "3 message(s) from 1 agent(s) over 0.0 hour(s) in '#my-channel'.",
  "message_count": 3,
  "participants": 1,
  "time_span_hours": 0.0
}
```

**Errors:** `CHANNEL_NOT_FOUND` (channel does not exist), `VALIDATION_ERROR` (empty channel name)

---

## Events

### Emitted

| Event | Payload | When |
|-------|---------|------|
| `chat.message.posted` | `channel`, `agent_id`, `message_id` | After a message is successfully inserted |

### Consumed

| Event | Handler | Action |
|-------|---------|--------|
| `agent.registered` | `_on_agent_registered_event` | Posts `"Agent {id} joined"` to `#general` |
| `agent.disconnected` | `_on_agent_disconnected_event` | Posts `"Agent {id} left"` to `#general` |

System messages are posted with `_agent_id = "system"`.

---

## Configuration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_message_length` | `int` | `100000` | Max characters per message |
| `max_channels` | `int` | `1000` | Max number of channels |
| `use_built_in_llm` | `bool` | `false` | Use built-in (stub) LLM for summaries |
| `llm_api_url` | `str` | `""` | OpenAI-compatible API endpoint |
| `llm_api_key` | `str` | `""` | Bearer token for the API |
| `llm_model` | `str` | `"gpt-4o-mini"` | Model name sent in API request |
| `llm_system_prompt` | `str` | `"Summarize the following chat messages concisely."` | System prompt for summarization |
| `llm_max_tokens` | `int` | `500` | Max tokens in LLM response |

Example plugin config:

```json
{
  "name": "chat",
  "enabled": true,
  "config": {
    "max_message_length": 100000,
    "max_channels": 1000,
    "llm_api_url": "https://api.openai.com/v1/chat/completions",
    "llm_api_key": "sk-...",
    "llm_model": "gpt-4o-mini"
  }
}
```

---

## Schema

### `chat_channels`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `TEXT` | `PRIMARY KEY` (UUID) |
| `name` | `TEXT` | `UNIQUE NOT NULL` (e.g. `#general`) |
| `topic` | `TEXT` | Optional |
| `metadata_json` | `TEXT` | Optional JSON blob |
| `created_at` | `TEXT` | `NOT NULL` (ISO 8601) |

### `chat_messages`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `TEXT` | `PRIMARY KEY` (UUID) |
| `channel_id` | `TEXT` | `NOT NULL` — FK to `chat_channels(id)` |
| `agent_id` | `TEXT` | `NOT NULL` — agent UUID or `"unknown"` / `"system"` |
| `parent_id` | `TEXT` | Optional — threading parent (advisory, no FK) |
| `content_type` | `TEXT` | Default `'text'` |
| `content` | `TEXT` | `NOT NULL` |
| `created_at` | `TEXT` | `NOT NULL` (ISO 8601) |

**Index:** `idx_messages_channel_time` on `chat_messages(channel_id, created_at)`

---

## Constraints (Never Violate)

1. **Messages are append-only.** No edit, deletion, or soft-delete. Once written, a message is immutable.
2. **No channel deletion.** Channels cannot be removed in v1.
3. **Auto-vivify on post only.** `read_messages` and `summarize_channel` never create channels — only `post_message` does.
4. **No raw SQL in tools.** All queries use prepared statements with `?` placeholders.
5. **No external HTTP dependencies.** LLM calls use `urllib` from stdlib.
6. **Channel names starting with `#`** is a convention, not enforced by the plugin — but `#general` is special-cased for event hooks and topic assignment.

## Testing

Tests live in `tests/test_plugins/test_chat.py` (42 tests across 8 classes). Run with:

```bash
pytest tests/test_plugins/test_chat.py -v
```

### Test Classes

| Class | Tests | Covers |
|-------|-------|--------|
| `TestPostMessage` | 9 | Post to new/existing channel, empty content, threading, max length, empty name, missing param, event emission, orphan parent_id |
| `TestReadMessages` | 8 | Existing/empty/non-existent channels, limit, limit=0, limit>1000 error, default params, desc order, since filter, invalid order, message shape |
| `TestListChannels` | 6 | Empty list, all channels, ordering, prefix filter, #general topic, message count + last activity |
| `TestSummarizeChannel` | 7 | Stats with messages, empty channel, existing empty, non-existent, empty name, built-in LLM stub, LLM API URL, single message, long name |
| `TestEventHooks` | 3 | Agent register posts to #general, agent disconnect posts to #general, multiple agents register |
| `TestEdgeCases` | 5 | Channel limit, concurrent posts, concurrent read-while-write, message ID uniqueness, message shape |

### Testing Patterns

Use in-memory DB with `skip_transport=True`:

```python
@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    srv = AgoraServer(
        config={"db_path": ":memory:", "plugins": [...]},
        skip_transport=True,
    )
    await srv.start()
    yield srv
    await srv.stop()
```

Call tools directly (bypasses auth):

```python
result = await server.call_tool("chat_post_message", {
    "channel": "#test", "content": "Hello",
})
assert result.get("message_id") is not None
```

For tests requiring `use_built_in_llm=True`, use the `llm_stub_server` fixture.
For tests requiring `llm_api_url`, use `patch("urllib.request.urlopen")`.

Event bus tests access `server._eventbus` directly (white-box, with `# noqa: SLF001`).
