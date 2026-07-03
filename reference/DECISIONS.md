# Decision Log

This file records architecture and implementation decisions that refine, deviate from, or clarify the original design documents. Each entry is tagged with the task that produced it.

---

## Task 001 — Backbone Scaffold, Plugin API, and Agent Registry

### 1. AgoraPlugin as Concrete Class (not ABC)

`AgoraPlugin` is a concrete class with default no-op implementations for all hooks. Plugins override only what they need. This avoids boilerplate and makes the base class importable without special metaclass handling.

### 2. ToolHandler Protocol

Tool handler functions must match the `ToolHandler` protocol:

```python
async def handler(self, *args: object, **kwargs: object) -> dict[str, object]: ...
```

This signature is required because FastMCP's `Tool.from_function()` creates explicit-parameter wrappers, but the underlying router dispatches to handlers via `**kwargs`.

### 3. FastMCP Native Middleware for Auth

Authentication is implemented as a FastMCP `Middleware.on_call_tool()` subclass, not as part of the router. The middleware intercepts every tool call before it reaches the router. Only the `register` tool bypasses the auth check — all other tools require a registered `agent_id`.

### 4. `_make_mcp_wrapper` for FastMCP Compatibility

FastMCP rejects functions with `**kwargs` in `Tool.from_function()`. The server creates explicit-parameter wrappers that accept a single `arguments: dict[str, object]` parameter and delegate to `router.route()`. This is handled by `_make_mcp_wrapper()` in `server.py`.

### 5. Backbone Tools Implemented

Five backbone tools are registered automatically (no plugin needed):

| Tool | Status | Notes |
|------|--------|-------|
| `register` | ✅ | Only unauthenticated tool |
| `heartbeat` | ✅ | Updates `last_heartbeat_at` on agent record |
| `list_agents` | ✅ | Returns all agents (no filter support in v1) |
| `get_agent` | ✅ | By agent UUID |
| `get_agent_by_name` | ✅ | By unique name |
| `set_status` | ❌ | Deferred — not in v1 |
| `find_agents` | ❌ | Deferred — not in v1 |
| `set_current_task` | ❌ | Deferred — not in v1 |

### 6. Database Schema (Actual vs Design)

| Table | Design | Actual | Notes |
|-------|--------|--------|-------|
| Agent records | `backbone_agents` | `agents` | Simplified — no `session_id` column |
| Sessions | `backbone_sessions` | ❌ Not implemented | Heartbeat tracking on agent record instead |
| Config | `backbone_config` | ✅ `backbone_config` | Used for migration hash tracking |
| Migration tracking | *(not specified)* | SHA-256 hashes in `backbone_config` | Each migration is hashed; applied if hash not found |

The `agents` table schema:

```sql
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    capabilities TEXT,
    manifest TEXT,
    current_task TEXT,
    last_heartbeat_at TEXT,
    registered_at TEXT NOT NULL
)
```

Notable omissions from the design doc:
- No `session_id` column on agent records (session tracking deferred)
- No separate `backbone_sessions` table
- No `connected_at` column (uses `registered_at` instead)
- No `token_budget` column (deferred)

### 7. Single APSW Async Connection (No Pool)

The backbone uses a single apsw SQLite connection wrapped with `asyncio.to_thread()` for async compatibility. There is no connection pool in v1. WAL mode enables concurrent reads+writes on the single connection.

### 8. Testing: `call_tool()` Bypasses Auth

The `AgoraServer.call_tool()` method bypasses both FastMCP transport and authentication, calling handlers directly on the router. This is intentional for testing. The `skip_transport=True` flag prevents stdio from starting during tests.

### 9. Plugin Lifecycle (Refined)

Actual startup sequence:

1. Database connect + WAL enable
2. Create EventBus
3. Create AgentRegistry + run migrations
4. Create PluginLoader
5. For each plugin:
   a. `on_load(config)` — parse config
   b. Execute plugin `get_migrations()` (tracked by SHA-256 hash)
   c. `on_startup()` — init connections, caches
   d. `get_tools()` — collect tool definitions
6. Register backbone tools on router
7. Register all plugin tools on router
8. Create FastMCP + AuthMiddleware
9. Register tools with FastMCP (via `_make_mcp_wrapper`)
10. Start stdio transport (unless `skip_transport`)

### 10. Plugin Loader Strategy

The plugin loader uses `importlib.import_module()` with naming convention: plugins are discovered under `agora.plugins.<name>`. The module must contain a class that subclass `AgoraPlugin`. Tools are collected by calling `get_tools()` on the instantiated plugin.

### 11. No `backbone_` Prefix on Backbone Tables

Tables owned by the backbone use short names (`agents`, `backbone_config`) rather than the design-doc `backbone_agents` prefix. This was a simplification choice — the backbone's ownership is implicit since it controls the database connection.
