# Backbone — Agent Instructions

## Overview

The backbone is a lightweight MCP server kernel that provides transport, identity, routing, and plugin lifecycle. It is **not** a monolithic server — plugins add all domain logic.

Key principle: **The backbone never calls an LLM.** That's plugin territory.

## Module Map

| File | Responsible for |
|------|----------------|
| `__init__.py` | `AgoraPlugin` base class, `ToolDef` dataclass, `ToolHandler` protocol |
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

Tool handlers **must** match the `ToolHandler` protocol:

```python
async def handler(self, *args: object, **kwargs: object) -> dict[str, object]:
    ...
```

This is required because FastMCP's tool registration creates explicit-parameter wrappers that delegate to `router.route()`, which calls handlers with `**kwargs`.

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
| `register` | `name`, `role?`, `capabilities?`, `manifest?` | `{"agent_id": "..."}` |
| `heartbeat` | `agent_id` | `{"ok": true}` |
| `list_agents` | *(none)* | `{"agents": [...]}` |
| `get_agent` | `agent_id` | `{"agent": {...} \| None}` |
| `get_agent_by_name` | `name` | `{"agent": {...} \| None}` |

`register` is the only unauthenticated tool — all others require a registered agent_id.

## Constraints (Never Violate)

1. **Backbone never calls an LLM.** Plugins may, but the backbone owns transport, identity, routing, and lifecycle.
2. **Every tool call is authenticated.** The router rejects unregistered agents before reaching any plugin.
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
