# 003 — Board Plugin

**Priority:** P0 | **Phase:** NOW | **Dependencies:** 001-backbone-scaffold

## What

The **Board plugin** implements the structured blackboard — a versioned key-value space per topic that agents use for shared decision-making. Unlike chat (free-form discussion), the blackboard is structured and versioned. It's a plugin loaded by the backbone at startup.

Tool prefix: `board_`

## Plugin Interface

```python
class BoardPlugin(AgoraPlugin):
    name = "board"
    version = "1.0.0"
    description = "Structured blackboard for agent coordination"

    def get_tools(self):
        return [
            ToolDef("board_create", self.create_topic, schema),
            ToolDef("board_write", self.write_entry, schema),
            ToolDef("board_read", self.read_entry, schema),
            ToolDef("board_history", self.read_history, schema),
            ToolDef("board_subscribe", self.subscribe, schema),
        ]

    def get_migrations(self):
        return [
            """CREATE TABLE board_topics (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                schema_json TEXT,
                created_at TEXT NOT NULL
            );""",
            """CREATE TABLE board_entries (
                id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (topic_id) REFERENCES board_topics(id)
            );""",
            """CREATE INDEX idx_entries_topic_key
                ON board_entries(topic_id, key);""",
        ]
```

## Tools

1. `board_create(topic_name, schema?)` → topic_id
   - Creates a new blackboard topic with optional JSON Schema validation
   - Schema validates all future writes to this topic
   - Emits: `board.topic.created`

2. `board_write(topic, key, value)` → version
   - Writes a key-value entry to the topic
   - Returns the new version number (monotonic counter per key)
   - Validates value against topic schema if one was declared
   - Throws on schema validation failure
   - Emits: `board.entry.written`

3. `board_read(topic, key?)` → entry | entries[]
   - Read a specific key or all keys in a topic
   - Returns current value(s) with version, updater agent, timestamp

4. `board_history(topic, key)` → versions[]
   - Returns full version history of a key

5. `board_subscribe(topic, pattern?)` → event stream
   - Server-side subscription for changes matching a key pattern
   - Used by agents that want to react to changes in real-time

## Events

**Emits:** `board.topic.created`, `board.entry.written` (payload: topic, key, agent_id, version)

**Consumes:** (none directly — other plugins read board entries via tools)

## Why This Matters

The blackboard is where structured decisions live:
- **`status` topic**: agent task statuses, progress, blockers
- **`decisions` topic**: consensus decisions with rationale
- **`scores` topic**: evaluation results for code/content quality
- **`plan` topic**: sprint plans, task breakdowns, assignments
- **`consensus` topic**: vote tallies and outcomes

This is the most important subsystem for scaling multi-agent work. PatchBoard (84.6% vs 30.8%) validates the blackboard pattern over chat-based coordination.

## Technical Notes

- Use JSON Schema draft-07 for validation (Zod in TS, jsonschema in Python)
- Schema is optional per topic — if not specified, any JSON is valid
- Version is a monotonically increasing integer per key-id, not per topic
- Consider a "compare and set" operation: `board_write_if(topic, key, expected_version, value)` for race-free consensus
- Keys support dot-notation for nested values (e.g., `status.agent-5`)
- PatchBoard-style JSON Patch mutations would be the ideal evolution but are deferred
- The Log plugin automatically records every tool call — no need to instrument logging here

## Relevant Context

- **ARCHITECTURE.md** — plugin system, event bus
- **PatchBoard** (arXiv:2605.29313): direct inspiration
- **LbMAS** (arXiv:2507.01701): SOTA blackboard approach
- **META.md** — research backing section
