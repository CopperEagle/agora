# The Agora

A plugin-based MCP coordination server for multi-agent collaboration. A lightweight Python backbone loads plugins (Chat, Board, Log, Memory, Lock/Signal) that provide tools for agents to coordinate through shared persistent state.

**Status:** Backbone complete — plugin development ready.

## Quick Start

```bash
# 1. Activate virtual environment
source venv/bin/activate

# 2. Install (dev mode)
uv sync  # or: pip install -e ".[dev]"

# 3. Run tests
pytest --cov
```

### Configuring in opencode.json

```jsonc
{
  "mcpServers": {
    "agora": {
      "command": "uv",
      "args": ["run", "python", "-m", "agora"],
      "transport": "stdio"
    }
  }
}
```

*Note: a `__main__.py` entry point will be added when the first plugin ships. For now, embed `AgoraServer` programmatically.*

## Configuration

The server accepts a config dict with these keys:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `db_path` | `str` | `"agora.db"` | Path to SQLite database |
| `plugins` | `list[dict]` | `[]` | Plugin configurations |

Each plugin config entry:

| Key | Type | Description |
|-----|------|-------------|
| `name` | `str` | Plugin module name (e.g. `"chat"`) |
| `enabled` | `bool` | Whether to load this plugin |
| `config` | `dict` | Plugin-specific configuration |

Example:

```python
server = AgoraServer({
    "db_path": "/path/to/agora.db",
    "plugins": [
        {"name": "chat", "enabled": True, "config": {"max_message_length": 100000}},
        {"name": "board", "enabled": True, "config": {}},
        {"name": "log", "enabled": True, "config": {"retention_days": 90}},
    ],
})
await server.start()
```

## Plugin Development

Plugins subclass `AgoraPlugin` and override the hooks they need:

```python
from agora.backbone import AgoraPlugin, ToolDef

class GreeterPlugin(AgoraPlugin):
    name = "greeter"
    version = "0.1.0"
    description = "A friendly greeter plugin"

    async def on_load(self, config: dict[str, object]) -> None:
        self.greeting = config.get("greeting", "Hello")

    async def on_startup(self) -> None:
        print(f"{self.name} started")

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="greet",
                handler=self._handle_greet,
                description="Greet someone by name",
            ),
        ]

    async def _handle_greet(self, *args: object, **kwargs: object) -> dict[str, object]:
        name = kwargs.get("name", "World")
        return {"message": f"{self.greeting}, {name}!"}
```

Plugin lifecycle (called by backbone):
1. `on_load(config)` — parse config, store settings
2. Migrations executed (if any returned by `get_migrations()`)
3. `on_startup()` — initialize connections, caches
4. Tools registered with router
5. `on_shutdown()` — clean up (called in reverse order on stop)

## Built-in Plugins

*Backbone complete — plugins pending implementation:*

- **Chat** — channels, messages, summarization (`chat_*` tools)
- **Board** — structured shared workspace (`board_*` tools)
- **Log** — activity audit, failure tracking, cost projection (`log_*` tools)
- **Lock/Signal** — resource locking, inter-agent signals (`lock_*` / `signal_*` tools)
- **Memory** — long-term key-value store with semantic search (`mem_*` tools)

See `reference/` for design documents for each plugin.

## Architecture

```
 FastMCP (stdio transport)
       │
 ┌─────▼──────┐
 │ AuthMiddleware │  ← only `register` is unauthenticated
 └─────┬──────┘
       │
 ┌─────▼──────────┐
 │  RequestRouter  │  ← dispatch + audit events
 └─────┬──────────┘
       │
 ┌─────▼──────┐   ┌──────────┐   ┌───────────┐
 │ Plugin API  │──▶│  Agent   │──▶│  EventBus │
 │ (AgoraPlugin)│   │ Registry │   │ (pub/sub) │
 └────────────┘   └──────────┘   └───────────┘
                        │
                  ┌─────▼──────┐
                  │  Database   │
                  │ (apsw+WAL) │
                  └────────────┘
```

The backbone owns transport, identity, routing, and plugin lifecycle. It never calls an LLM. See [`reference/ARCHITECTURE.md`](reference/ARCHITECTURE.md) for full detail.

## Testing

```bash
pytest                          # all tests
pytest -v tests/test_backbone/  # backbone tests only
pytest -k "concurrent"          # concurrency tests
pytest --cov --cov-fail-under=90
```

Current coverage: **98%** across all production modules (110 tests).

## Performance Targets

| Metric | Target | How |
|--------|--------|-----|
| Backbone startup | < 100ms | Lazy-load plugins, compiled migrations |
| Tool call (no DB) | < 1ms | Minimal routing, no per-call imports |
| Tool call (DB read) | < 5ms | Indexed queries, prepared statements |
| Tool call (DB write) | < 10ms | WAL mode, batch commits |
| Concurrent agents | 10+ | Single async apsw connection |

## Quality Gates

Before merging any change:

1. `ruff check .` — zero warnings
2. `mypy --strict .` — zero type errors
3. `pytest --cov --cov-fail-under=90` — coverage threshold
4. No `print()` or `logging.debug()` in production code
5. Tool inputs validated by Pydantic schema (no manual validation)
6. Every public function has a Google-style docstring
