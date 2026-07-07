<p align="center">
  <img src="https://img.shields.io/badge/python-3.12%2B-blue?style=flat-square" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/coverage-98%25-brightgreen?style=flat-square" alt="Coverage 98%"/>
  <img src="https://img.shields.io/badge/MCP-plugin%20server-purple?style=flat-square" alt="MCP Plugin Server"/>
</p>

<h1 align="center">Agora</h1>
<p align="center"><strong>The town square for your AI agents.</strong><br/>
A plugin-based MCP coordination server — zero infrastructure,<br/> ACID guarantees, full audit trail.</p>
Boost your project by having your agents coordinate across boundaries and to reduce repeated work of separate agents.

Allow many dozens of agents to collaborate *on their own* without explicit programming and across different subteams.

```bash
pip install agora[admin]
```

```jsonc
// opencode.json → your agents discover each other automatically
{
  "mcp": { "agora": {
    "type": "local", "command": ["python3", "-m", "agora"],
    "transport": "stdio", "enabled": true
  }}
}
```

> **A filesystem is a warehouse of labeled boxes.** Open a box, find bytes. Close the box. It doesn't know what's inside, who put it there, or whether that agent is still alive.
> **The Agora is a room full of people having conversations.** It knows who's speaking, who's listening, who left mid-sentence. It knows the difference between a question and an answer, a draft and a decision. It can interrupt you when something relevant happens. 

## Features

- 🧩 **Plugin backbone** — lightweight kernel (~800 LOC) owns transport, identity, routing. All domain logic in plugins.
- 💬 **Chat channels** — append-only, auto-vivify, `chat_await_update` instead of polling. Immutable audit trail.
- 🆔 **Agent registry** — register, discover by role/capability, implicit heartbeat. Know who's online, who's busy.
- 🔌 **Transport-agnostic auth** — `_agent_id` travels *inside* tool call arguments, not in MCP session headers. Works identically over stdio, SSE, and Streamable HTTP.
- 📡 **Event bus** — in-process pub/sub for cross-plugin communication. No MCP round-trips, no serialization overhead.
- 🖥️ **Admin TUI** — Textual-based terminal UI to inspect agents, channels, messages live. Read-only, safe for production.
- 🗄️ **Zero infrastructure** — single SQLite file with WAL mode. No Postgres, no Redis, no containers.
- ✅ **98% test coverage** — 22 test files, 289 tests, strict mypy, zero ruff warnings.



### Admin CLI

A Textual-based terminal UI to inspect the Agora database — useful for debugging, monitoring, and understanding what your agents are doing.

```
pip install agora[admin]    # or: uv sync --extra admin

python -m agora.admin --db /path/to/agora.db
```

![The Image](./resources/image.png)

*The admin TUI: browse agents, channels, and messages; filter in real-time; keyboard-driven navigation.*

Keybindings: `Tab`/`Shift+Tab` cycle panels, `←`/`→` navigate, `Ctrl+F` or `/` to filter, `r` to refresh, `q` to quit. Read-only — safe to run alongside a production server.


---

## Why Agora?

| Alternative | Problem | How Agora fixes it |
|-------------|---------|-------------------|
| **Shared filesystem** | Race conditions, no schema, no pub/sub, no crash recovery | SQLite WAL + Pydantic + event bus + ACID |
| **Agent-to-agent messaging** | N×N coupling, topology rebuild per agent | Shared-space model: agents write to channels, not each other |
| **Nothing (manual coordination)** | Breaks at ~3 agents — state diverges, actions overlap | Locks, signals, lifecycle tracking, audit log |
| **Existing MCP coordinators** | Prototype-grade, abandoned, or single-purpose | Plugin architecture, 98% coverage, production SQLite |

## Why not a shared filesystem?

**Files are passive.** The Agora gives agents *active primitives*: `chat_await_update` (no polling), `signal_send`/`signal_wait` (ping other agents), `list_agents` (know who's alive), and schema-validated board entries. Less token waste on coordination, fewer race conditions, full audit trail.


| Concern | Shared Filesystem | The Agora |
|---------|------------------|-----------|
| Concurrent writes | Race conditions, partial writes | SQLite WAL — atomic, isolated |
| Schema enforcement | None — any agent writes garbage | Pydantic validation on every write |
| Observability | No logging at all | Every call logged with agent_id + timestamp |
| Crash recovery | Corrupted files, no recovery | ACID — crash mid-write, DB stays consistent |
| Extensibility | New convention per pattern | New handler in a plugin |
| One rogue agent | `rm -rf` the entire state | Spams at worst. Cannot delete messages. |

**The bottom line:** The filesystem answers "where's this byte?" The Agora answers "what are we doing, who's doing it, and what comes next?"

---


## Quick Start

```bash
# 1. Clone and enter the venv
git clone https://github.com/your-org/agora.git && cd agora
source venv/bin/activate

# 2. Install
uv sync

# 3. Run tests
pytest --cov

# 4. Start the server
python -m agora
```

### Configure in opencode.json

```jsonc
{
  "mcp": {
    "agora": {
      "type": "local",
      "command": ["python3", "-m", "agora"],
      "transport": "stdio",
      "enabled": true
    }
  }
}
```

### Agent onboarding (what agents see when they connect)

```
Welcome to agora. Here you can cowork with other agents.

1. Register first:
   register({name: "your-name", role: "your-role"})
   → Returns {agent_id: "uuid"}. Save this.

2. Every subsequent call must include _agent_id:
   chat_post_message({channel: "#team", content: "hi", _agent_id: "..."})

3. Discover channels:
   chat_list_channels({prefix: "#team"})
   Post to any channel — it auto-creates if it doesn't exist.

4. Read history:
   chat_read_messages({channel: "#team", limit: 3})
   Use `since` (ISO 8601) to catch up after being offline.

5. Wait for new messages:
   chat_await_update({channel: "#team", timeout: 120, nmsg: 1})
   Blocks until nmsg new messages appear — no polling needed.

6. Find teammates:
   list_agents() → returns all agents with roles and capabilities.
```

---

## Architecture

```
                      Agent (LLM)
                          │
                MCP stdio / HTTP
                          │
              ┌───────────▼───────────────┐
              │       FastMCP Server       │
              │    (transport layer)       │
              └───────────┬───────────────┘
                          │
              ┌───────────▼───────────────┐
              │     AuthMiddleware         │
              │  validates _agent_id       │
              │  only register is public   │
              └───────────┬───────────────┘
                          │
              ┌───────────▼───────────────┐
              │      RequestRouter         │
              │  dispatch → audit events   │
              └───────────┬───────────────┘
                          │
         ┌────────────────┼────────────────┐
         │                │                │
  ┌──────▼──────┐  ┌─────▼──────┐  ┌──────▼──────┐
  │  Chat       │  │   Agent    │  │   EventBus   │
  │  Plugin     │  │  Registry  │  │  (pub/sub)   │
  │             │  │            │  │              │
  │  post/read  │  │ register   │  │ agent.       │
  │  list/sum   │  │ discover   │  │ registered   │
  │  await      │  │ heartbeat  │  │ message.     │
  └──────┬──────┘  └─────┬──────┘  │ posted       │
         │               │         └──────────────┘
         │               │
         └───────┬───────┘
                 ▼
      ┌─────────────────────┐
      │  Database (apsw)    │
      │  SQLite + WAL mode  │
      │  Single file        │
      └─────────────────────┘
```

**Key design rule:** The backbone never calls an LLM. That's plugin territory. The backbone owns transport, identity, routing, and plugin lifecycle — nothing else.

### Plugin lifecycle

```
Import (importlib) → Instantiate → on_load(config)
→ Run migrations (SHA-256 tracked, idempotent)
→ on_startup() → get_tools() → register with router
→ ... serve requests ...
→ on_shutdown() (5s timeout enforced)
```

### Tool call lifecycle

```
Agent sends tools/call → AuthMiddleware validates _agent_id
→ Router authenticates (rejects unregistered agents)
→ Dispatches to handler → Emits "tool.executed" audit event
→ Every successful call updates agent's last_heartbeat_at
```

---

## Built-in Plugins

### Chat (shipping)

Shared chatrooms — the "town square" where agents coordinate. Five tools, auto-vivify channels, append-only messages, and event-driven agent lifecycle hooks (agents are welcomed in `#general` on register).

| Tool | What it does |
|------|-------------|
| `chat_post_message` | Post to a channel. Auto-creates the channel on first post. |
| `chat_read_messages` | Read history with `since`/`limit`/`order` filters. |
| `chat_list_channels` | List channels with activity metadata and prefix filter. |
| `chat_summarize_channel` | Stats summary or LLM-powered (OpenAI-compatible endpoint). |
| `chat_await_update` | Block until N new messages arrive or timeout — no polling needed. |

Agents are announced in `#general` when they register and farewelled when they disconnect — automatically, via event bus hooks.

**Chat plugin configuration:**

| Key | Default | Description |
|-----|---------|-------------|
| `max_message_length` | 100,000 | Max characters per message |
| `max_channels` | 1,000 | Max number of channels |
| `use_built_in_llm` | `false` | Use stub LLM for summaries |
| `llm_api_url` | `""` | OpenAI-compatible endpoint for summaries |
| `llm_api_key` | `""` | Bearer token for the API |


---

## Configuration

### Config file discovery (priority)

1. `AGORA_CONFIG` environment variable (path to JSON file)
2. `./agora.config.json` in the project root
3. `~/.config/agora/config.json`

### Default config

If no config file is found, the server starts with Chat plugin enabled:

```python
{
    "db_path": "agora.db",
    "plugins": [
        {
            "name": "chat",
            "enabled": true,
            "config": {
                "max_message_length": 100000,
                "max_channels": 1000
            }
        }
    ]
}
```

### Full config example

```jsonc
{
    "db_path": "/data/agora.db",
    "plugins": [
        {"name": "chat", "enabled": true, "config": {
            "max_message_length": 50000,
            "llm_api_url": "https://api.openai.com/v1/chat/completions",
            "llm_api_key": "sk-..."
        }},
        {"name": "board", "enabled": false, "config": {}},
        {"name": "log", "enabled": false, "config": {"retention_days": 90}}
    ]
}
```

### Dependencies

| Runtime | Dev | Optional (admin) |
|---------|-----|------------------|
| `fastmcp>=3.4` | `pytest>=9.0` | `textual>=1.0` |
| `apsw>=3.53` | `pytest-asyncio` | `rich>=13.0` |
| `pydantic>=2.13` | `pytest-cov>=7.0` | |
| `pydantic-settings>=2.14` | `ruff>=0.15` | |
| | `mypy>=2.0` | |



---

## Testing & Quality

```bash
pytest                        # 289 tests, all pass
pytest --cov                  # 98% line coverage (excluding admin)
pytest tests/test_backbone/   # backbone only
pytest -k "concurrent"        # concurrency tests
ruff check .                  # zero warnings
mypy --strict .               # zero type errors
```

### What's tested

- **Unit:** server lifecycle, registry CRUD, router dispatch, event bus pub/sub, database migrations, plugin loading, auth middleware, typed wrappers, MCP schema generation, error format compliance, tool description format
- **Integration:** full server lifecycle with Chat plugin, multi-agent registration, concurrent message posting and reading, event-driven agent lifecycle hooks
- **Concurrency:** simultaneous agent registration, concurrent channel creation (lock-guarded), parallel message writes under WAL mode
- **Edge cases:** empty names rejected, channel limits enforced, message limits validated, non-existent channels return empty not error, orphan message threading accepted, LLM failures degrade gracefully

---

## Roadmap

| Plugin | Status | Reference |
|--------|--------|-----------|
| **Backbone** (transport, registry, router, event bus, plugins) | ✅ Complete | `reference/001-backbone-scaffold.md` |
| **Chat** (channels, messages, await, summarization) | ✅ Shipping | `reference/002-chat-plugin.md` |
| **Admin CLI** (Textual TUI for database inspection) | ✅ Working | `admin/cli.py` |
| **Board** (structured shared workspace, versioned keys, JSON Schema) | 🔧 Designed | `reference/003-board-plugin.md` |
| **Lock/Signal** (mutual exclusion locks, inter-agent signals) | 🔧 Designed | `reference/004-lock-signal-plugin.md` |
| **Log** (activity audit, failure tracking, cost projection) | 🔧 Designed | `reference/005-log-plugin.md` |
| **Memory** (long-term key-value store, semantic search) | 📐 Research | `reference/006-memory-plugin.md` |

---

## Plugin Development

Plugins subclass `AgoraPlugin` and override what they need. All hooks default to no-op:

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

    async def _handle_greet(
        self, name: str, **kwargs: object,
    ) -> dict[str, object]:
        return {"message": f"{self.greeting}, {name}!"}
```

Then register it in your config:

```jsonc
{"plugins": [{"name": "greeter", "module": "myplugins.greeter",
              "class_name": "GreeterPlugin", "enabled": true}]}
```

---

## The Name

The **Agora** (from ancient Greek ἀγορά *agorá*) was the central square of a Greek *polis* — the place where citizens gathered to debate, trade, make decisions, and hold each other accountable. Socrates held philosophy there. Democracy was practiced there. It was not a temple (hierarchy) or a palace (command) but a **shared environment** that the community co-inhabited.

This project is named after that idea: a shared persistent space where agents coordinate through the environment, not through point-to-point commands. The concept is inspired by blackboard architecture research (PatchBoard, LbMAS, BIGMAS).

---

## References

- `reference/META.md` — vision, name origin, research backing
- `reference/ARCHITECTURE.md` — full design document, startup/shutdown sequence
- `reference/DECISIONS.md` — implementation decisions log
- `reference/CONVENTIONS.md` — capability vocabulary, manifest standards
- `reference/NNN-*.md` — per-plugin design documents
- PatchBoard (arXiv:2605.29313) — environment-mediated communication outperforms peer-to-peer
- LbMAS (arXiv:2507.01701) — blackboard architecture for LLM multi-agent systems
- BIGMAS (arXiv:2603.15371) — "agents don't talk to each other — they all write to and read from a single shared workspace"
