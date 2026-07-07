# Backbone — Agent Instructions

## Overview

The backbone is a lightweight MCP server kernel that provides transport, identity, routing, and plugin lifecycle. It is **not** a monolithic server — plugins add all domain logic.

Key principle: **The backbone never calls an LLM.** That's plugin territory.

## Module Map

| File | Responsible for |
|------|----------------|
| `__init__.py` | `AgoraPlugin` base class, `ToolDef` dataclass, `ToolHandler` protocol (typed params) |
| `database.py` | APSW async SQLite wrapper, WAL mode, SHA-256 migration runner |
| `eventbus.py` | In-process pub/sub (`subscribe`/`emit`/`unsubscribe`) |
| `registry.py` | Agent register/heartbeat/status/discovery (DB-persisted) |
| `loader.py` | Plugin discovery via `importlib`, three-phase startup |
| `router.py` | Tool registration, agent auth check, dispatch, audit events |
| `middleware.py` | FastMCP Middleware subclass — intercepts calls, checks auth |
| `server.py` | `AgoraServer` — assembles everything, owns lifecycle |

## Plugin API (AgoraPlugin)

All lifecycle hooks default to no-op — override only what you need:

```python
from agora.backbone import AgoraPlugin, ToolDef

class MyPlugin(AgoraPlugin):
    name = "myplugin"       # Unique, used as namespace
    version = "0.1.0"
    description = "..."

    async def on_load(self, config: dict[str, object]) -> None:
        ...  # Parse config

    async def on_startup(self) -> None:
        ...  # Init connections, caches

    async def on_shutdown(self) -> None:
        ...  # Cleanup (5s timeout enforced by backbone)

    async def on_agent_register(self, agent_id: str) -> None:
        ...  # React to new agent

    async def on_agent_disconnect(self, agent_id: str) -> None:
        ...  # React to agent leaving

    def get_tools(self) -> list[ToolDef]:
        return [ToolDef(
            name="my_tool",          # Will be prefixed: "myplugin_my_tool"
            handler=self._handler,
            description="...",
        )]

    def get_migrations(self) -> list[str]:
        return ["CREATE TABLE IF NOT EXISTS ..."]
```

### Tool Handler Signature

Tool handlers use **typed parameters** (not `**kwargs**`). The `_agent_id` parameter is synthetically included in every tool's MCP schema as an optional parameter — FastMCP's Pydantic validation needs it declared to accept it. The `AuthMiddleware` validates and injects the authenticated value before dispatch:

```python
async def handler(self, channel: str, content: str) -> dict[str, object]:
    """Post a message to a channel.

    Use this when agents need to communicate or announce results.
    Channels auto-create on first post.
    """
    ...
```

`_make_typed_wrapper()` preserves the handler's type annotations and adds `_agent_id` as a synthetic optional parameter so FastMCP can auto-generate the `inputSchema`. The middleware validates `_agent_id` from the raw tool arguments; the typed wrapper then passes it as `session_id` to the router for the second auth check.

### Database Access

The backbone provides a single APSW async connection. Plugins share it — **do not create your own connections or connection pools.**

- Use `database.execute(sql, params)` for writes
- Use `database.fetch(sql, params)` for single-row reads
- Use `database.fetch_all(sql, params)` for multi-row reads
- All SQL via prepared statements (`?` placeholders only) — never string formatting

```python
await database.execute(
    "INSERT INTO my_table (id, value) VALUES (?, ?)",
    ["uuid-123", "hello"],
)
```

### Event Bus

Plugins communicate through the backbone's in-process EventBus — **not** through direct method calls or shared tables.

```python
# Subscribe
eventbus.subscribe("agent.registered", self._on_agent_registered)

# Emit
await eventbus.emit("myplugin.something_happened", key="value")
```

Standard event names: `agent.registered`, `agent.disconnected`, `tool.executed`.

## Backbone Tools (Always Available)

These tools are registered on the router automatically — no plugin needed:

| Tool | Parameters | Returns |
|------|-----------|---------|
| `register` | `name: str`, `role?: str`, `capabilities?: list[str]`, `manifest?: dict` | `{"agent_id": "..."}` |
| `list_agents` | *(none)* | `{"agents": [...]}` |
| `get_agent` | `agent_id: str` | `{"agent": {...} \| None}` |
| `get_agent_by_name` | `name: str` | `{"agent": {...} \| None}` |

`register` is the only unauthenticated tool — all others require a registered `_agent_id` (extracted from arguments by `AuthMiddleware`).

Heartbeat is now **implicit**: every authenticated tool call updates the agent's `last_heartbeat_at` timestamp automatically after the handler completes. No explicit heartbeat tool is needed. The lifecycle manager uses these timestamps to detect stale agents and mark them offline.

## Constraints (Never Violate)

1. **Backbone never calls an LLM.** Plugins may, but the backbone owns transport, identity, routing, and lifecycle.
2. **Every tool call is authenticated.** The router rejects unregistered agents before reaching any plugin. Every authenticated call also serves as an implicit heartbeat — `last_heartbeat_at` is updated automatically after the handler completes, so agents don't need to call a separate heartbeat tool.
3. **Plugins own their tables.** Never read another plugin's tables directly — use the event bus.
4. **Tool names are prefixed** per plugin namespace (e.g., `chat_post_message`).
5. **No raw SQL** in tool handlers — always use prepared statements.
6. **Single SQLite database** — one `agora.db` with WAL mode. No separate log or vector DB in v1.

## Quality Gates

Before submitting backbone changes:

```bash
ruff check .
mypy --strict .
pytest --cov --cov-fail-under=90
```

- Zero ruff warnings
- Zero mypy strict errors (explicit `Any` prohibited)
- 90%+ line coverage
- No `# type: ignore` / `# noqa` unless genuinely unavoidable for mypy strict
- All public methods have Google-style docstrings

## Development

```bash
source venv/bin/activate
pytest -v tests/test_backbone/    # backbone tests only (no plugin tests)
```

Virtual environment is `venv/` in project root — never use system Python or a different path.

## Testing Patterns

### Unit tests (fast, no transport)

Use `server.call_tool()` to invoke handlers directly (bypasses auth):

```python
result = await server.call_tool("register", {"name": "test-agent"})
agent_id = str(result["agent_id"])

# Typed parameters — matches the handler signature exactly
result = await server.call_tool("chat_post_message", {
    "channel": "general",
    "content": "hello",
})
```

### Integration tests (in-memory database)

```python
server = AgoraServer(config={
    "db_path": ":memory:",
    "plugins": [],
}, skip_transport=True)
await server.start()
# ... test ...
await server.stop()
```

### Schema validation

Tool schemas are auto-generated from typed parameters. To verify a tool's schema:

```python
tool = server.get_tool("chat_post_message")
schema = tool.inputSchema
assert "channel" in schema["properties"]
assert "content" in schema["properties"]
assert "_agent_id" in schema["properties"]  # synthetic optional param
```
