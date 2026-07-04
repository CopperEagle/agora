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

---

## Task 002 — Chat Plugin

### 1. Tool Prefix Applied by Server, Not Plugin

The design doc shows tools as `ToolDef("chat_post_message", …)` with the prefix hardcoded. In the implementation, plugins define short names (`post_message`, `read_messages`, etc.) and the server prepends the prefix at registration time in `server.py`:

```python
for plugin in self._plugins:
    for tool_def in plugin.get_tools():
        self._router.register_tool(
            tool_def.name, tool_def.handler, prefix=plugin.name,
        )
```

`router.register_tool()` concatenates as `f"{prefix}_{name}"` when a prefix is provided. This eliminates duplication across plugins and ensures every future plugin gets namespacing automatically. The design doc's interface sketch (showing prefixed names in `ToolDef`) was updated — plugins now declare unprefixed names and the backbone handles the rest.

### 2. `limit` Validation: Strict Bounds, Not Clamping

`read_messages` accepts `limit` in `[0, 1000]`. Values outside this range return `VALIDATION_ERROR` rather than being silently clamped — the caller must know their input was invalid. `limit = 0` is valid (returns an empty `messages` list); SQLite `LIMIT 0` works naturally.

### 3. `_agent_id` Injection for Authenticated Calls

The design doc's implicit assumption that "real calls include the agent_id" was not implemented by any component. Two changes were made:

- **Middleware** (`middleware.py`): After authenticating the caller, injects `_agent_id` into `context.message.arguments` before forwarding to the handler. This is the primary path for real MCP calls.
- **Router** (`router.py`): When `route()` successfully authenticates a `session_id`, it also injects `_agent_id` into handler kwargs — a belt-and-suspenders measure covering any future caller that passes a real `session_id`.

`call_tool()` (testing path) bypasses both middleware and `route()`, calling `handler(**args)` directly. Tests always receive `"unknown"` for `_agent_id`.

### 4. `summarize_channel`: LLM Integration with Graceful Degradation

The `summarize_channel` tool has three modes, forming a graceful degradation chain:

| Mode | Trigger | Behaviour |
|------|---------|-----------|
| **Custom LLM** | `llm_api_url` configured | Makes an OpenAI-compatible `POST` via `urllib` (stdlib, no extra dependencies). Sends the last 50 messages formatted as a conversation. Handles URL scheme validation (`http`/`https` only), auth headers, and parse errors gracefully — never raises, always returns a summary string. |
| **Built-in stub** | `use_built_in_llm = True` | Returns a stats string prefixed with "Built-in LLM summarization not yet implemented." This is a placeholder for a future free/embedded LLM. |
| **Stats fallback** | Neither configured | Returns a plain-text summary with message count, unique participants, and time span. |

**Key principle: `summarize_channel` never returns an error when the LLM is unavailable.** No LLM endpoint configured → stats fallback. LLM call fails (network error, bad response) → the error is logged and a descriptive string is returned in place of the summary. The tool always returns a well-formed response.

### 5. `_ensure_channel` Uses `asyncio.Lock` for Concurrency Safety

The auto-vivify path (check-then-create) is guarded by `asyncio.Lock` to prevent concurrent `SELECT`-then-`INSERT` races for the same channel name. Without this, two agents posting to a new channel simultaneously could both see "channel not found" and both attempt `INSERT` — the second would hit the `UNIQUE` constraint on `chat_channels.name`. The lock serialises these operations per-plugin-instance.

### 6. `parent_id` Is Advisory (No FK Enforcement)

The `parent_id` column on `chat_messages` accepts any UUID string, even if it references a non-existent message. There is no foreign key constraint on `parent_id` — the design doc's `FOREIGN KEY` only applies to `channel_id`. Orphan `parent_id`s are silently accepted; the field is purely advisory for clients to display threading.

### 7. Event Hooks via Event Bus Subscriptions

The `ChatPlugin` does not rely on the backbone calling `on_agent_register()`/`on_agent_disconnect()` directly. Instead, it subscribes to `agent.registered` and `agent.disconnected` events on the `EventBus` during `on_startup()`. The subscription handlers parse the event payload and delegate to the hook methods. This decouples the plugin from the backbone's lifecycle dispatch — any component that emits these events triggers the chat notifications.

### 8. Database Schema (Actual vs Design Doc)

| Table/Column | Design Doc | Actual | Notes |
|--------------|------------|--------|-------|
| `chat_channels` | ✅ | ✅ | All columns match |
| `chat_messages` | ✅ | ✅ | All columns match, including `parent_id`, `content_type` |
| `idx_messages_channel_time` | ✅ | ✅ | Index on `(channel_id, created_at)` |
| `FOREIGN KEY (parent_id)` | ❌ Not specified | ❌ No FK | Advisory only (see entry 6) |
| `content_type DEFAULT 'text'` | ❌ Not specified | ✅ Implemented | Always `'text'` in v1 |

### 9. Config Keys (Actual vs Design Doc)

| Key | Design Doc | Actual | Default | Notes |
|-----|------------|--------|---------|-------|
| `max_message_length` | ✅ Mentioned (100K) | ✅ | 100,000 | — |
| `max_channels` | ✅ Mentioned | ✅ | 1000 | — |
| `use_built_in_llm` | ❌ Not specified | ✅ | `False` | Stub only (see entry 4) |
| `llm_api_url` | ✅ "configurable LLM endpoint" | ✅ | `""` | OpenAI-compatible |
| `llm_api_key` | ❌ Not specified | ✅ | `""` | Sent as Bearer token |
| `llm_model` | ✅ "configurable model" | ✅ | `"gpt-4o-mini"` | — |
| `llm_system_prompt` | ✅ "configurable system prompt" | ✅ | `"Summarize the following chat messages concisely."` | — |
| `llm_max_tokens` | ✅ "budget limits" | ✅ | 500 | — |

The three keys not in the design doc (`use_built_in_llm`, `llm_api_key`, `max_channels`) were added during implementation. `max_channels` was mentioned in the technical notes but not listed as a config key; `llm_api_key` is a practical requirement for real API calls; `use_built_in_llm` gates the stub implementation.

---

## Task 002.5 — MCP Usability: Typed Wrappers, Auth Fix, Tool Schemas

### 1. Synthetic `_agent_id` in Typed Wrapper Signature

**Context:** FastMCP's `Tool.from_function()` creates a Pydantic model from the tool function's `__signature__`. When an agent includes `_agent_id` in tool call arguments (required by `AuthMiddleware` for authentication), Pydantic rejects it as an unexpected parameter if `_agent_id` is not declared in the signature.

**Decision:** `_make_typed_wrapper()` in `server.py` now synthetically adds `_agent_id` as a `KEYWORD_ONLY` parameter with `default=None` to every tool's wrapper signature. This ensures:

1. **FastMCP acceptance**: Pydantic validation passes because `_agent_id` is a declared optional parameter.
2. **Schema visibility**: `_agent_id` appears in every tool's `inputSchema` as an optional property, making it discoverable by agents.
3. **Auth propagation**: The typed wrapper extracts `_agent_id` from kwargs and passes it as `session_id` to `router.route()` for the second-layer auth check.

**Auth flow (updated):**

```
Agent call → FastMCP recv → AuthMiddleware (extracts _agent_id from raw args,
validates against AgentRegistry, injects validated value back into args)
→ FastMCP Pydantic validation (accepts _agent_id because declared in schema)
→ typed_wrapper(**kwargs) → router.route(tool_name, kwargs, session_id=_agent_id)
→ router.authenticate(session_id) → handler(**args)
```

**`_agent_id` is always optional** in the schema (`default=None`) — it is never required. The `register` tool is the only tool that works without it; all others return `NOT_AUTHORIZED` if `_agent_id` is missing or invalid.

**Key implication for plugin authors:** Plugin handlers do NOT need to declare `_agent_id` in their typed parameters. The wrapper adds it automatically. Handlers can safely ignore `_agent_id` — it's handled by middleware and router.
