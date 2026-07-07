"""Integration tests for the full Agora server lifecycle.

Exercises the complete AgoraServer lifecycle with multiple plugins
and concurrent agents, verifying component interaction end-to-end.
"""

from __future__ import annotations

import asyncio
import importlib
import types
from collections.abc import AsyncGenerator

import pytest

from agora.backbone import AgoraPlugin, ToolDef
from agora.backbone.server import AgoraServer

# ── Mock plugin for lifecycle testing ──────────────────────────


class _LifecyclePlugin(AgoraPlugin):
    """Plugin that tracks lifecycle calls for testing."""

    name = "lifecycle"
    version = "1.0.0"
    description = "Tracks lifecycle calls"

    def __init__(self) -> None:
        """Initialize lifecycle tracking state."""
        self.load_config: dict[str, object] | None = None
        self.startup_called = False
        self.shutdown_called = False

    async def on_load(self, config: dict[str, object]) -> None:
        """Capture the config passed during loading."""
        self.load_config = config

    async def on_startup(self) -> None:
        """Mark that startup was called."""
        self.startup_called = True

    async def on_shutdown(self) -> None:
        """Mark that shutdown was called."""
        self.shutdown_called = True

    def get_tools(self) -> list[ToolDef]:
        """Return a ping tool for testing."""
        return [
            ToolDef(name="ping", handler=self._ping, description="Ping handler"),
        ]

    async def _ping(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        """Return a pong response."""
        return {"pong": True}

    def get_migrations(self) -> list[str]:
        """Return a simple migration for testing."""
        return ["CREATE TABLE IF NOT EXISTS lifecycle_data (id TEXT PRIMARY KEY)"]


def _make_lifecycle_module() -> types.ModuleType:
    """Create a temporary module containing the _LifecyclePlugin class."""
    mod = types.ModuleType("_lifecycle_plugin_module")
    mod.ChatPlugin = _LifecyclePlugin  # type: ignore[attr-defined]
    return mod


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
async def empty_server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a running AgoraServer with no plugins for basic tests."""
    srv = AgoraServer(config={"db_path": ":memory:", "plugins": []}, skip_transport=True)
    await srv.start()
    yield srv
    await srv.stop()


# ── Tests ─────────────────────────────────────────────────────


async def test_full_lifecycle(empty_server: AgoraServer) -> None:
    """Start → register → list → get → stop."""
    result = await empty_server.call_tool("register", {"name": "test-agent"})
    assert "agent_id" in result

    agents = await empty_server.call_tool("list_agents", {})
    agents_list = agents["agents"]
    assert isinstance(agents_list, list)
    assert len(agents_list) == 1
    first_agent = agents_list[0]
    assert isinstance(first_agent, dict)
    assert first_agent["name"] == "test-agent"

    agent = await empty_server.call_tool(
        "get_agent", {"agent_id": result["agent_id"]},
    )
    agent_data = agent["agent"]
    assert isinstance(agent_data, dict)
    assert agent_data["name"] == "test-agent"


async def test_concurrent_registrations(empty_server: AgoraServer) -> None:
    """Register 3 agents simultaneously with asyncio.gather()."""
    results = await asyncio.gather(
        empty_server.call_tool("register", {"name": "agent-a"}),
        empty_server.call_tool("register", {"name": "agent-b"}),
        empty_server.call_tool("register", {"name": "agent-c"}),
    )

    agent_ids = [str(r["agent_id"]) for r in results]
    assert len(set(agent_ids)) == 3, "All agent IDs must be unique"

    agents = await empty_server.call_tool("list_agents", {})
    agents_list = agents["agents"]
    assert isinstance(agents_list, list)
    assert len(agents_list) == 3

    for agent in agents_list:
        assert isinstance(agent, dict)
        assert agent["status"] == "offline"


async def test_agent_reregistration(empty_server: AgoraServer) -> None:
    """Register agent 'alice' twice with different role — same id, updated role."""
    result1 = await empty_server.call_tool(
        "register", {"name": "alice", "role": "scout"},
    )
    agent_id1 = str(result1["agent_id"])

    result2 = await empty_server.call_tool(
        "register", {"name": "alice", "role": "reviewer"},
    )
    agent_id2 = str(result2["agent_id"])

    assert agent_id1 == agent_id2, "Re-registration must return the same agent_id"

    agent = await empty_server.call_tool("get_agent", {"agent_id": agent_id1})
    agent_data = agent["agent"]
    assert isinstance(agent_data, dict)
    assert agent_data["role"] == "reviewer"


async def test_tool_call_audit_events(empty_server: AgoraServer) -> None:
    """Subscribe to tool.executed on EventBus, call a tool via router, verify event."""
    received_events: list[dict[str, object]] = []

    async def _on_tool_executed(**kwargs: object) -> None:
        received_events.append(kwargs)

    reg = await empty_server.call_tool("register", {"name": "audit-agent"})
    agent_id = str(reg["agent_id"])

    assert empty_server.eventbus is not None
    empty_server.eventbus.subscribe("tool.executed", _on_tool_executed)

    assert empty_server._router is not None  # noqa: SLF001
    await empty_server._router.route(  # noqa: SLF001
        "list_agents", {}, session_id=agent_id,
    )

    assert len(received_events) == 1
    assert received_events[0]["tool"] == "list_agents"


async def test_plugin_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify on_load → on_startup → tool available → on_shutdown."""
    mod = _make_lifecycle_module()
    original_import = importlib.import_module

    def _patched_import(name: str) -> types.ModuleType:
        if name == "_lifecycle_plugin_module":
            return mod
        return original_import(name)

    monkeypatch.setattr(importlib, "import_module", _patched_import)

    cfg: dict[str, object] = {
        "db_path": ":memory:",
        "plugins": [
            {
                "name": "lifecycle",
                "enabled": True,
                "module": "_lifecycle_plugin_module",
                "class_name": "ChatPlugin",
                "config": {"key": "value"},
            },
        ],
    }
    srv = AgoraServer(config=cfg, skip_transport=True)
    await srv.start()

    try:
        assert len(srv.plugins) == 1
        plugin = srv.plugins[0]
        assert isinstance(plugin, _LifecyclePlugin)
        assert plugin.load_config == {"key": "value"}
        assert plugin.startup_called is True
        assert plugin.shutdown_called is False

        result = await srv.call_tool("lifecycle_ping", {})
        assert result == {"pong": True}
    finally:
        await srv.stop()

    assert plugin.shutdown_called is True


async def test_error_handling(empty_server: AgoraServer) -> None:
    """Unknown tool → structured error dict, empty list, unknown agent → None."""
    result = await empty_server.call_tool("nonexistent_tool", {})
    assert result["error"] == "TOOL_NOT_FOUND"
    assert "fix" in result

    agents = await empty_server.call_tool("list_agents", {})
    assert agents["agents"] == []

    agent = await empty_server.call_tool(
        "get_agent", {"agent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert agent["agent"] is None


async def test_concurrent_read_write(empty_server: AgoraServer) -> None:
    """Two agents register and list agents concurrently."""
    result_a = await empty_server.call_tool("register", {"name": "writer-a"})
    result_b = await empty_server.call_tool("register", {"name": "writer-b"})

    agents_a, agents_b = await asyncio.gather(
        empty_server.call_tool("list_agents", {}),
        empty_server.call_tool("list_agents", {}),
    )

    agents_a_list = agents_a["agents"]
    agents_b_list = agents_b["agents"]
    assert isinstance(agents_a_list, list)
    assert isinstance(agents_b_list, list)
    assert len(agents_a_list) == 2
    assert len(agents_b_list) == 2

    names = {str(result_a["agent_id"]), str(result_b["agent_id"])}
    listed_ids = {str(a["id"]) for a in agents_a_list if isinstance(a, dict)}
    assert names == listed_ids
