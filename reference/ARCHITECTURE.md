# The Agora — Architecture

## Design: Plugin-Based Backbone

The Agora is **not a monolithic server**. It's a lightweight backbone that provides transport, identity, and routing — plus a plugin API that subsystems plug into. This ensures extensibility, testability, and separation of concerns.

## Layer Overview

```
 ┌──────────────────────────────────────────────────────────────┐
 │                     TRANSPORT LAYER                           │
 │  MCP stdio transport   │   MCP Streamable HTTP transport      │
 │  (default, simplest)   │   (for remote agents)                │
 └──────────────────────────────┬───────────────────────────────┘
                                │
 ┌──────────────────────────────▼───────────────────────────────┐
 │                     BACKBONE (CORE)                           │
 │                                                               │
 │  ┌─────────────────┐  ┌─────────────────┐                     │
 │  │  Agent Registry  │  │  Request Router  │                    │
 │  │  • register      │  │  • authenticate  │                    │
 │  │  • heartbeat     │  │  • route-to-plugin│                    │
 │  │  • identity      │  │  • error handling │                    │
 │  │  • capabilities  │  │                   │                    │
 │  └─────────────────┘  └─────────────────┘                     │
 │                                                               │
 │  ┌─────────────────┐  ┌─────────────────┐                     │
 │  │  Lifecycle Manager│  │  Config Store   │                    │
 │  │  • session tracking│  │  • plugin prefs │                    │
 │  │  • token budgets  │  │  • server config │                    │
 │  │  • disconnection  │  │  • agent prefs  │                    │
 │  └─────────────────┘  └─────────────────┘                     │
 │                                                               │
 └──────────────────────────────┬───────────────────────────────┘
                                │
 ┌──────────────────────────────▼───────────────────────────────┐
 │                     PLUGIN API                                 │
 │                                                               │
 │  A plugin registers:                                          │
 │    • One or more MCP tool implementations                     │
 │    • Hooks: on_startup, on_shutdown, on_agent_register,       │
 │             on_agent_disconnect, on_tool_call                  │
 │    • Its own database tables (namespaced)                     │
 │                                                               │
 │  A plugin can call:                                           │
 │    • Backbone: whoami(), list_agents(), get_config()          │
 │    • Other plugins: via shared event bus (signals)            │
 │                                                               │
 └──────────────────────────────┬───────────────────────────────┘
                                │
 ┌──────────┬───────────┬───────────┬───────────┬───────────────┐
 │  CHAT    │  BOARD    │  LOG      │  MEMORY   │  CUSTOM        │
 │  Plugin  │  Plugin   │  Plugin   │  Plugin   │  Plugin(s)     │
 │          │           │           │           │  (third-party) │
 │  post_   │  board_   │  record_  │  store_   │                │
 │  message │  write    │  event    │  recall   │                │
 │  read_   │  board_   │  query_   │  search_  │                │
 │  messages│  read     │  log      │  memory   │                │
 └──────────┴───────────┴───────────┴───────────┴───────────────┘

 ┌──────────────────────────────────────────────────────────────┐
 │                  SHARED INFRASTRUCTURE                         │
 │                                                               │
 │  • SQLite database (WAL mode) — each plugin gets a namespace  │
 │  • Event bus (in-memory signals) — cross-plugin notification  │
 │  • Lock manager — prevent concurrent access to resources      │
 │  • Configuration — JSON config file for server + plugins      │
 │                                                               │
 └──────────────────────────────────────────────────────────────┘
```

## Backbone Responsibilities

### Agent Registry (Core — Not a Plugin)

The registry is part of the backbone because identity must be established **before** any tool call reaches a plugin.

**Data per agent:**
- `agent_id` (server-generated, unique)
- `name` (self-declared, human-readable)
- `role` (e.g., "reviewer", "scout", "planner")
- `capabilities` (array of strings, e.g. `["code:review", "web:search"]`)
- `manifest` (free-form JSON for agent-specific metadata)
- `status` ("online" | "idle" | "busy" | "offline")
- `session_id` (current session, regenerated on reconnect)
- `connected_at`, `last_heartbeat_at`
- `token_budget` (optional cap per session)

**Lifecycle:**
1. Agent connects → MCP session starts
2. Agent calls `register(name, role?, capabilities?, manifest?)`
3. Backbone creates agent record, returns `agent_id`
4. Agent calls `heartbeat()` every N minutes (default 5)
5. Agent calls `set_status(status)` and `set_current_task(task)` as it works
6. On disconnect or 3 missed heartbeats → status = "offline"
7. Agent can register again with same name → gets same `agent_id` (if within TTL) or new one

**Backbone tools (always available, no plugin needed):**
- `register(name, role?, capabilities?, manifest?)` → agent_id
- `heartbeat()` → refreshes session
- `set_status(status, task?)` → updates agent state
- `list_agents(filter?)` → agents matching filter
- `find_agents(capability)` → agents with a specific capability
- `get_agent(agent_id?)` → agent info (own by default)

### Request Router

Every MCP tool call passes through the router:

1. **Authenticate**: Extract agent_id from session. Reject if unregistered (except for `register` tool).
2. **Route**: Map tool name to plugin. Each plugin registers a prefix or explicit list of tool names.
3. **Execute**: Call the plugin's tool handler with authenticated context (agent_id, session_id included).
4. **Audit**: After execution, backbone logs the call (tool, agent, duration, outcome) to the Log plugin.
5. **Respond**: Return result to agent.

If a tool name conflicts across plugins, the first-registered plugin wins (or configurable priority).

### Lifecycle Manager

Tracks sessions, enforces token budgets, handles disconnection:

- **Session tracking**: Every connection gets a session_id. Tool calls are tagged with it.
- **Token budget enforcement**: If an agent has a budget cap, the lifecycle manager tracks cumulative usage and rejects calls when exceeded (gracefully: "You've used 48K of 50K budget").
- **Graceful shutdown**: On server shutdown, all plugins get `on_shutdown` hook.
- **Heartbeat monitoring**: Periodic check for stale agents; moves them to "offline".

### Config Store

A simple key-value store (SQLite table) for server and plugin configuration:

- Server config: transport type, port (for HTTP), heartbeat interval, log level
- Plugin config: per-plugin settings (e.g., Chat plugin: max message length)
- Agent preferences: agents can store per-agent config (e.g., "preferred model")

## Plugin API

### Plugin Interface

A plugin is a module that implements:

```python
class AgoraPlugin:
    name: str                        # Unique plugin name
    version: str                     # Semver
    description: str                 # Human-readable

    # Backbone calls these:
    on_load(config: dict) -> None    # Plugin loaded, configure self
    on_startup() -> None             # Server starting, initialize
    on_shutdown() -> None            # Server shutting down, clean up

    # Hooks into backbone events:
    on_agent_register(agent_id: str) -> None
    on_agent_disconnect(agent_id: str) -> None

    # Must return list of (tool_name, handler_fn, schema):
    get_tools() -> list[ToolDef]

    # Optional: database migrations (SQL executed on load):
    get_migrations() -> list[str]
```

### Plugin Registration

Plugins are registered in the server config:

```jsonc
// agora_config.json
{
  "backbone": {
    "transport": "stdio",
    "heartbeat_interval_sec": 300
  },
  "plugins": [
    {"name": "chat", "enabled": true, "config": {"max_message_length": 100000}},
    {"name": "board", "enabled": true, "config": {}},
    {"name": "log", "enabled": true, "config": {"retention_days": 90}},
    {"name": "memory", "enabled": false, "config": {}},
    {"name": "locks", "enabled": true, "config": {}}
  ]
}
```

### Cross-Plugin Communication

Plugins communicate via the **event bus** (in-memory, not MCP):

- `emit(event_name, payload)` — broadcast to all plugins
- `on(event_name, handler)` — subscribe to events
- Events are typed: `agent.registered`, `agent.disconnected`, `message.posted`, `board.updated`, `lock.acquired`

This prevents circular dependencies: Log plugin listens to `message.posted` from Chat, but Chat doesn't call Log directly.

## Built-in Plugins

### Chat Plugin

**Namespace:** `chat_` prefix on tool names
**Tools:** `chat_post_message`, `chat_read_messages`, `chat_list_channels`, `chat_summarize_channel`
**Tables:** `chat_channels`, `chat_messages`
**Events emitted:** `chat.message.posted`, `chat.channel.created`
**Events consumed:** (none)

### Board Plugin

**Namespace:** `board_` prefix
**Tools:** `board_create`, `board_write`, `board_read`, `board_history`, `board_subscribe`
**Tables:** `board_topics`, `board_entries`
**Events emitted:** `board.entry.written`, `board.topic.created`

### Log Plugin

**Namespace:** `log_` prefix
**Tools:** `log_record`, `log_query`, `log_summary`, `log_costs`
**Tables:** `activity_log`, `failure_log`, `cost_projections`
**Events consumed:** All events (logs every tool call automatically via backbone audit)

### Memory Plugin

**Namespace:** `mem_` prefix
**Tools:** `mem_store`, `mem_recall`, `mem_search`, `mem_forget`
**Tables:** `memory_entries`, `memory_tags`
**Description:** Long-term key-value memory with semantic search. Agents can store facts, preferences, learned patterns. Not just short-term coordination (that's Chat/Board) but persistent knowledge.

### Lock/Signal Plugin

**Namespace:** `lock_` and `signal_` prefixes
**Tools:** `lock_acquire`, `lock_release`, `lock_status`, `signal_send`, `signal_wait`
**Tables:** `locks`, `signals`
**Note:** This is used by other plugins too — Log uses locks for atomic writes, Board uses them for conflict resolution.

## Database Architecture

Single SQLite database (`agora.db`) with per-plugin table namespacing:

```
agora_registry          ← Backbone
agora_config            ← Backbone  
agora_sessions          ← Backbone
chat_channels           ← Chat plugin
chat_messages           ← Chat plugin
board_topics            ← Board plugin
board_entries           ← Board plugin
activity_log            ← Log plugin
failure_log             ← Log plugin
memory_entries          ← Memory plugin
locks                   ← Lock plugin
signals                 ← Signal plugin
```

WAL mode enabled for concurrent reads+writes. Each plugin manages its own migrations.

## Startup Sequence

1. Load config file
2. Initialize SQLite, enable WAL mode
3. Load backbone (registry, router, lifecycle manager)
4. For each enabled plugin in config:
   a. Call `on_load(config)` — plugin parses its config
   b. Run plugin migrations
   c. Call `on_startup()` — plugin initializes
   d. Register plugin's tools with router
5. Start transport (stdio or HTTP server)
6. Accept connections, route tool calls

## Shutdown Sequence

1. Stop accepting new connections
2. For each plugin (reverse order): call `on_shutdown()`
3. Flush and close SQLite
4. Exit

## Why Plugin Architecture?

| Concern | Monolithic | Plugin-based |
|---------|-----------|--------------|
| Extensibility | Add code to the server | Write a plugin, register it |
| Testability | Test the whole server | Test each plugin in isolation |
| Replaceability | Rewrite the component | Swap the plugin |
| Third-party contributions | Fork the repo | Write a plugin |
| Configuration | Everything or nothing | Enable/disable per plugin |
| Startup time | Load everything | Load only enabled plugins |
| Failure isolation | One crash takes down all | Plugin crash doesn't affect others (with isolation) |
