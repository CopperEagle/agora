# AGENTS.md — The Agora

## Overview

The Agora is a plugin-based MCP coordination server. A lightweight Python backbone loads plugins (Chat, Board, Log, Memory, Lock/Signal) that provide tools for multi-agent collaboration via SQLite-backed MCP endpoints.

All design documents live in the `reference/` directory. Read these before writing any code:
- `reference/META.md` — vision, why MCP, name origin
- `reference/ARCHITECTURE.md` — plugin API, backbone responsibilities, startup sequence
- The relevant `reference/NNN-*.md` task file(s) for what you're implementing
- `reference/CONVENTIONS.md` — capability vocabulary, manifest standards
- `reference/AGENTS.md` — this file (coding standards, testing, workflow)

## Technology Decisions (Settled)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | **Python 3.12+** | Simple plugin model (import + class), mature SQLite ecosystem (sqlite-vec, apsw), pytest for testing |
| MCP Framework | **FastMCP** | Clean tool definition, built-in schema validation, stdio + Streamable HTTP support |
| Database | **SQLite with WAL mode** (via apsw) | Zero infrastructure, concurrent reads+writes, sqlite-vec for vector search later |
| Testing | **pytest + pytest-asyncio** | Standard for Python async servers, rich fixture system |
| Linting | **ruff** | Fast, covers flake8 + isort + pyupgrade, single dependency |
| Type Checking | **mypy (strict)** | Catch type errors before runtime |
| Packaging | **uv** (or pip) | Simple single-package install, no build step |
| Virtual Environment | **`./venv/`** (in project root) | Must use this exact path. All commands assume `source venv/bin/activate`. Never use system Python or a differently-named venv. |

## Architecture Constraints

These must never be violated:

1. **Backbone never calls an LLM.** That's plugin territory. The backbone owns transport, identity, routing, and plugin lifecycle. No exceptions.
2. **Every tool call is authenticated.** The backbone rejects unregistered agents before routing to any plugin.
3. **Plugins own their tables.** Plugin A cannot read Plugin B's database tables directly. Communication is via the event bus only.
4. **Tool names are prefixed.** `chat_`, `board_`, `log_`, `mem_`, `lock_`, `signal_`. No prefix collisions.
5. **The event bus is in-process (not MCP).** Plugins emit and subscribe to Python events. No serialization overhead.
6. **The database is single-file.** One `agora.db` with WAL mode. No separate log database. No external vector database in v1.

## Testing Requirements

Production-grade coverage. Test the following categories:

### Unit Tests (fast, no MCP transport)

Every function that isn't a trivial getter/setter:

- **Plugin loading**: Config parsing, `on_load` → `on_startup` sequence, migration execution, tool registration
- **Agent registry**: Register → heartbeat → status update → timeout → offline. Re-registration with same name.
- **Request router**: Authenticate → route → execute → audit event. Reject unregistered. Handle unknown tool.
- **Event bus**: Subscribe → emit → receive. Multiple subscribers. Unsubscribe. No memory leak on subscriber crash.
- **Each plugin's tool logic**: Post message, read messages, board write/read/validate, lock acquire/release/expiry, signal send/wait/clear, log query/filter/costs

### Integration Tests (with in-memory transport)

A full server lifecycle with mock MCP transport:

```python
async def test_chat_plugin_end_to_end():
    server = AgoraServer(config={"plugins": [{"name": "chat", "enabled": true}]})
    await server.start()

    agent_id = await server.call_tool("register", {"name": "test-agent"})
    msg_id = await server.call_tool("chat_post_message", {"channel": "general", "content": "hello"})
    messages = await server.call_tool("chat_read_messages", {"channel": "general"})

    assert len(messages) == 1
    assert messages[0]["content"] == "hello"

    await server.stop()
```

Test combinations of plugins together:
- Chat + Board + Log: post message → verify it appears in activity log
- Lock + Signal: acquire lock on resource → signal completion → no conflicts

### Concurrency Tests

Critical for a multi-agent server. SQLite WAL mode supports this but the server must too:

- Two agents registering simultaneously → unique IDs, no conflicts
- Two agents writing to the same board key → both succeed at different versions
- Lock acquired by Agent A → Agent B tries to acquire same lock → B gets null or waits
- Signal sent while multiple agents wait → exactly one agent receives it (or all, depending on design)
- Plugin `on_startup` while another plugin is already serving requests
- Concurrency tests should be loops trying several times to ensure correctness

### Edge Cases

- Register with empty name → graceful rejection
- Post message to non-existent channel (auto-vivify vs. reject — document the choice)
- Board write with value that violates schema → clear error message identifying the violation
- Lock TTL expires while agent holds it → lock auto-released, next acquire succeeds
- Log query with no results → empty list, not error
- All plugin migrations re-run on existing database → no-op (idempotent)

## Performance Targets

| Metric | Target | How |
|--------|--------|-----|
| Backbone startup | < 100ms | Lazy-load plugins, compile migrations on first access |
| Tool call latency (no DB) | < 1ms | Minimal routing overhead, no per-call plugin import |
| Tool call latency (with DB read) | < 5ms | Indexed queries, prepared statements, connection reuse |
| Tool call latency (with DB write) | < 10ms | WAL mode, batch commits where safe |
| Concurrent agents | 10+ | Single SQLite connection with busy timeout, async event loop |
| Plugin load time | < 50ms each | No heavy imports in `on_load` (defer to first use) |
| Memory per idle agent | < 1MB | No per-agent threads, no per-agent state in backbone |

Never block the async event loop. All SQLite operations use `asyncio.to_thread` or a dedicated apsw connection with async wrapper.

## Code Conventions

### Style
- Ruff with default rules (E, F, I, N, W, UP)
- Line length: 100
- Type hints on all function signatures, including `-> None` where there's no return
- Docstrings: Google style on all public methods, optional on private

### Plugin Structure

```
agora/
├── backbone/
│   ├── __init__.py          # AgoraPlugin base class, ToolDef dataclass
│   ├── server.py            # AgoraServer — load config, plugins, start transport
│   ├── registry.py          # AgentRegistry — register, heartbeat, list, find
│   ├── router.py            # RequestRouter — authenticate, dispatch, audit
│   └── eventbus.py          # EventBus — in-process pub/sub
├── plugins/
│   ├── chat/
│   │   ├── __init__.py      # ChatPlugin class
│   │   ├── models.py        # Pydantic models for tool inputs/outputs
│   │   └── migrations.py    # SQL migration strings
│   ├── board/               # Same structure
│   ├── log/                 # Same structure
│   ├── locks/               # Same structure
│   └── memory/              # Same structure
├── tests/
│   ├── test_backbone/
│   ├── test_plugins/
│   └── conftest.py          # Shared fixtures (in-memory server, mock transport)
├── reference/               # Design documents (META, ARCHITECTURE, tasks, CONVENTIONS)
├── .gitignore               # Must be maintained — add venv/, __pycache__, *.db, .ruff_cache, .mypy_cache
├── pyproject.toml
├── README.md
└── AGENTS.md                # This file (stays at top level)
```

### Error Handling
- All tool errors return structured dicts, not raw exceptions: `{"error": "LOCK_NOT_FOUND", "message": "No lock with id X", "details": {...}}`
- The backbone wraps every plugin tool call in a try/except. If a plugin raises, the backbone returns a generic error and logs the traceback via the Log plugin.
- Plugin authors must not catch `BaseException` — let the backbone handle shutdown signals.

### SQL
- All SQL strings are in `migrations.py` files as module-level constants
- No raw SQL in tool handlers — always use prepared statements
- Table names and column names use `snake_case`
- Every table has an `id TEXT PRIMARY KEY` (UUID strings, not auto-increment integers)

## Quality Gates

Before considering a task done:

1. `ruff check .` — zero warnings
2. `mypy --strict .` — zero type errors
3. `pytest --cov --cov-fail-under=90` — 90%+ line coverage
4. All integration tests pass (3+ concurrent simulated agents)
5. No `print()` or `logging.debug()` left in production code (use `logging.info` sparingly)
6. Tool inputs validated by Pydantic/FastMCP schema (no manual validation)
7. Every public function has a Google-style docstring
8. `.gitignore` covers all generated artifacts: `venv/`, `__pycache__/`, `*.db`, `.ruff_cache/`, `.mypy_cache/`, `*.pyc`, `dist/`, `*.egg-info/`
9. The relevant README section exists (see Documentation below)

## Documentation

### README.md Sections (Required)

```
# The Agora

## Quick Start
Install → configure in opencode.json → run → agents connect.
(5 lines, copy-paste commands)

## Configuration
Every config key with its default. Plugin configs documented under their own headers.

## Plugin Development
```python
from agora.backbone import AgoraPlugin

class MyPlugin(AgoraPlugin):
    ...
```
One complete example plugin (15-20 lines).

## Built-in Plugins
For each plugin: tools, config options, example usage.
Chat → Board → Log → Lock/Signal → Memory.

## Architecture
Brief layers diagram (3-line ASCII). Reference to ARCHITECTURE.md for full detail.

## Testing
```bash
pytest                    # all tests
pytest tests/backbone/    # backbone only
pytest -k "concurrent"    # concurrency tests
```

## Performance
Expected latency numbers (from the table above). How to profile.
```

### Docstrings (Required on all public API)

Every method on `AgoraPlugin`, every tool handler, every backbone public method:

```python
def register(self, name: str, role: str | None = None,
             capabilities: list[str] | None = None,
             manifest: dict | None = None) -> str:
    """Register an agent with the backbone.

    Args:
        name: Human-readable agent name. Must be unique.
        role: Optional role string (e.g. "reviewer", "scout").
        capabilities: List of capability strings (see CONVENTIONS.md).
        manifest: Free-form JSON metadata.

    Returns:
        Server-generated agent_id UUID.

    Raises:
        ValueError: If name is empty or already taken.
        RuntimeError: If backbone is not running.
    """
```

## Security Checklist

- [ ] No SQL injection: all queries use parameterized statements (`?` placeholders)
- [ ] No arbitrary file access: tool paths are validated against allowed prefixes
- [ ] Agent identity is server-attached, not agent-declared (agent can't fake another's ID)
- [ ] Lock TTL prevents deadlocks if agent crashes while holding a lock
- [ ] Plugin `on_shutdown` has a timeout (5s) — plugin can't hang server shutdown
- [ ] Config file is read-only at runtime (no plugin writes its own config after startup)
- [ ] Token budgets are enforced server-side (agent can't report lower usage than actual)

## Development Workflow

0. **Activate the virtual environment** — `source venv/bin/activate`. If `venv/` doesn't exist, create it with `uv venv` or `python3 -m venv venv`, then install dependencies with `uv sync` or `pip install -e ".[dev]"`. All subsequent commands assume the venv is active.
1. Read the task file (`NNN-*.md`) completely
2. Read ARCHITECTURE.md for the relevant section
3. Write tests first (unit + integration) for the new functionality
4. Implement until tests pass
5. Run full lint + typecheck + coverage
6. Update README if adding/changing a user-facing feature
7. Before PR: re-read the task file's acceptance criteria and verify each one

## Migration Policy

Since the Agora stores persistent data in SQLite, schema changes must be backward-compatible:

- **Additive changes** (new table, new column with default): Safe, auto-migrate
- **Destructive changes** (drop table, remove column): Require a migration script, never auto-execute
- **Renames**: Add new table/column, migrate data, keep old as deprecated for one version, remove in next

Each plugin's `get_migrations()` returns an ordered list of SQL strings. New migrations are appended. The backbone tracks which migrations have been applied in the `backbone_config` table.

## Dependencies

Keep the dependency footprint small:

| Package | Why |
|---------|-----|
| `fastmcp` | MCP server framework |
| `apsw` | Advanced SQLite wrapper (better than stdlib sqlite3 for concurrency) |
| `pydantic` | Schema validation for tools |
| `uvloop` | Faster asyncio event loop (optional, enable on Linux) |

Dev-only:
| Package | Why |
|---------|-----|
| `pytest` | Testing framework |
| `pytest-asyncio` | Async test support |
| `pytest-cov` | Coverage reporting |
| `ruff` | Linting |
| `mypy` | Type checking |
