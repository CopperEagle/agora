# 001 — Backbone Scaffold, Plugin API, and Agent Registry

**Priority:** P0 | **Phase:** NOW | **Dependencies:** None

## What

Create the Agora **backbone** — the core server that provides transport, the plugin loading system, the agent registry, and the request router. This is NOT a monolithic server. It's a lightweight kernel that plugins (Chat, Board, Log, Lock/Signal, custom) plug into.

See `ARCHITECTURE.md` for the full design rationale.

## Acceptance Criteria

### 1. Server Scaffold and Plugin Loader

The server starts as an MCP server (stdio or Streamable HTTP), configurable in `opencode.json`. On startup:

1. Load config file (`agora_config.json` or embedded in opencode.json)
2. Initialize SQLite database at `~/.config/opencode/agora/agora.db` (configurable)
3. Enable WAL mode for concurrent reads+writes
4. Load plugin list from config
5. For each enabled plugin:
   - Call `plugin.on_load(plugin_config)` — plugin parses its config
   - Execute plugin's database migrations (namespaced tables)
   - Call `plugin.on_startup()` — plugin initializes
   - Register plugin's tools with the router (tool names are prefixed per plugin)
6. Start transport and accept connections

### 2. Backbone-Only Database Tables

The backbone owns only three tables. Everything else is managed by plugins:

```sql
-- Agent identity and tracking
CREATE TABLE backbone_agents (
    id TEXT PRIMARY KEY,          -- server-generated UUID
    name TEXT UNIQUE NOT NULL,    -- human-readable, unique
    role TEXT,                    -- "reviewer", "scout", etc.
    capabilities TEXT,            -- JSON array of strings
    manifest_json TEXT,           -- free-form JSON
    status TEXT DEFAULT 'online', -- online | idle | busy | offline
    session_id TEXT,              -- current session
    connected_at TEXT,            -- ISO 8601
    last_heartbeat_at TEXT,
    token_budget INTEGER          -- optional cap per session
);

-- Active sessions (regenerated on reconnect)
CREATE TABLE backbone_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    transport TEXT,               -- "stdio" | "http"
    connected_at TEXT,
    disconnected_at TEXT,
    FOREIGN KEY (agent_id) REFERENCES backbone_agents(id)
);

-- Server and plugin configuration
CREATE TABLE backbone_config (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
```

### 3. Agent Registry (Built into Backbone)

The registry is part of the backbone because identity must be established **before** any tool call reaches a plugin. This is not a plugin.

**Lifecycle:**
1. Agent connects → MCP session starts
2. Agent calls `register(name, role?, capabilities?, manifest?)` → agent_id
3. Backbone creates agent record, returns agent_id
4. Agent calls `heartbeat()` every N minutes (default 5)
5. Agent calls `set_status(status)` and `set_current_task(task)` as it works
6. On disconnect or 3 missed heartbeats → status = "offline"

**Backbone tools (always available, no plugin needed):**
- `register(name, role?, capabilities?, manifest?)` → agent_id
- `heartbeat()` → refreshes session
- `set_status(status, task?)` → updates agent state
- `list_agents(filter?)` → agents matching filter
- `find_agents(capability)` → agents with a specific capability
- `get_agent(agent_id?)` → agent info (own by default)

### 4. Plugin API

A plugin is a module that implements this interface:

```python
class AgoraPlugin:
    name: str                        # Unique, e.g. "chat"
    version: str                     # Semver
    description: str

    # Lifecycle hooks (called by backbone):
    on_load(config: dict) -> None    # Plugin loaded, configure self
    on_startup() -> None             # Server starting, run migrations + init
    on_shutdown() -> None            # Server shutting down, clean up

    # Backbone event hooks:
    on_agent_register(agent_id: str) -> None
    on_agent_disconnect(agent_id: str) -> None

    # Tool registration:
    get_tools() -> list[ToolDef]     # Each ToolDef has name, handler, schema

    # Database migrations (executed in order on startup):
    get_migrations() -> list[str]    # SQL statements
```

Plugins are registered in the server config:

```jsonc
{
  "backbone": {
    "transport": "stdio",
    "heartbeat_interval_sec": 300
  },
  "plugins": [
    {"name": "chat", "enabled": true, "config": {"max_message_length": 100000}},
    {"name": "board", "enabled": true, "config": {}},
    {"name": "log", "enabled": true, "config": {"retention_days": 90}},
    {"name": "locks", "enabled": true, "config": {}}
  ]
}
```

### 5. Request Router

Every MCP tool call passes through the backbone router:

1. **Authenticate**: Extract agent_id from session. Reject if unregistered (except for `register`).
2. **Route**: Map tool name to the plugin that registered it.
3. **Execute**: Call the plugin's handler with authenticated context (agent_id, session_id).
4. **Audit**: After execution, backbone emits `tool.executed` event. The Log plugin (if enabled) picks this up automatically.
5. **Respond**: Return result to agent.

### 6. Event Bus (Cross-Plugin Communication)

The backbone provides an in-memory event bus for plugin-to-plugin communication (no MCP involved):

- `emit(event_name, payload)` — broadcast to all plugins
- `on(event_name, handler)` — subscribe to events

Standard event types: `agent.registered`, `agent.disconnected`, `tool.executed`, plus plugin-specific events like `chat.message.posted`, `board.entry.written`.

## Why This Task First

This is the kernel. Plugins have nothing to plug into without it. The agent registry is in the backbone because identity precedes every action — a tool call from an unregistered agent is rejected at the router before any plugin sees it.

## Technical Notes

- Language choice: Python (FastMCP) or TypeScript. Python preferred for simpler plugin model (import + class). TypeScript for tighter MCP integration.
- Tool prefix convention: Each plugin's tools are prefixed (e.g., `chat_post_message`). The router uses the prefix to dispatch. No prefix collisions allowed.
- Plugin isolation: Each plugin gets its own SQLite table namespace (`chat_*`, `board_*`, etc.) and can't access other plugins' tables directly — only via the event bus.
- Backbone is small: ~200 lines without plugins, ~400 with registry and router. It should be embeddable.
- The backbone never calls LLMs. That's plugin territory.

## Relevant Context

- **ARCHITECTURE.md** — full design document, layer descriptions, startup/shutdown sequence, plugin API
- **META.md** — vision, research backing, open questions
- **opencode.json** in project root — for MCP server configuration
