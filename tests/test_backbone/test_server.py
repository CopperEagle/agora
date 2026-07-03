"""Tests for AgoraServer — assemble all backbone components."""

from __future__ import annotations

import importlib
import types
from collections.abc import AsyncGenerator

import pytest

from agora.backbone import AgoraPlugin, ToolDef
from agora.backbone.eventbus import EventBus
from agora.backbone.server import AgoraServer

# ── Mock plugin for testing ─────────────────────────────────────


class _EchoPlugin(AgoraPlugin):
    """Minimal plugin that registers one tool."""

    name = "echo"
    version = "1.0.0"
    description = "Echo plugin for testing"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="echo_ping",
                handler=self._ping,
                description="Ping handler",
            ),
        ]

    async def _ping(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"pong": True}


# ── Helpers ─────────────────────────────────────────────────────


def _make_echo_module() -> types.ModuleType:
    """Create a temporary module containing the _EchoPlugin class."""
    mod = types.ModuleType("_echo_plugin_module")
    mod.ChatPlugin = _EchoPlugin  # type: ignore[attr-defined]
    return mod


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def server_cfg() -> dict[str, object]:
    """Minimal in-memory server config with no plugins."""
    return {
        "db_path": ":memory:",
        "plugins": [],
    }


@pytest.fixture
async def server(server_cfg: dict[str, object]) -> AsyncGenerator[AgoraServer, None]:
    """Provide a started AgoraServer with skip_transport=True."""
    srv = AgoraServer(config=server_cfg, skip_transport=True)
    await srv.start()
    yield srv
    await srv.stop()


# ── Tests ───────────────────────────────────────────────────────


async def test_server_start_stop(server_cfg: dict[str, object]) -> None:
    """Given a minimal config, When starting and stopping the server,
    Then no errors are raised."""
    srv = AgoraServer(config=server_cfg, skip_transport=True)
    await srv.start()
    await srv.stop()


async def test_server_with_db_path() -> None:
    """Given a config with db_path=':memory:', When starting the server,
    Then the database is connected to that path."""
    cfg: dict[str, object] = {"db_path": ":memory:", "plugins": []}
    srv = AgoraServer(config=cfg, skip_transport=True)
    await srv.start()
    assert srv.database is not None
    await srv.stop()


async def test_call_tool_register(server: AgoraServer) -> None:
    """Given a running server, When calling the register tool,
    Then an agent_id is returned."""
    result = await server.call_tool("register", {"name": "alice"})
    assert "agent_id" in result
    assert isinstance(result["agent_id"], str)
    assert len(result["agent_id"]) == 36


async def test_call_tool_heartbeat(server: AgoraServer) -> None:
    """Given a registered agent, When calling heartbeat,
    Then ok=True is returned."""
    reg_result = await server.call_tool("register", {"name": "bob"})
    agent_id = str(reg_result["agent_id"])
    result = await server.call_tool("heartbeat", {"agent_id": agent_id})
    assert result["ok"] is True


async def test_call_tool_list_agents(server: AgoraServer) -> None:
    """Given a fresh server, When listing agents,
    Then an empty list is returned."""
    result = await server.call_tool("list_agents", {})
    assert result["agents"] == []


async def test_server_skip_transport() -> None:
    """Given skip_transport=True, When starting the server,
    Then the server starts without binding to stdio."""
    cfg: dict[str, object] = {"db_path": ":memory:", "plugins": []}
    srv = AgoraServer(config=cfg, skip_transport=True)
    await srv.start()
    assert srv.skip_transport is True
    await srv.stop()


async def test_server_stores_config() -> None:
    """Given a config dict, When creating the server,
    Then the config is accessible."""
    cfg: dict[str, object] = {
        "db_path": ":memory:",
        "plugins": [{"name": "chat", "enabled": True}],
    }
    srv = AgoraServer(config=cfg, skip_transport=True)
    assert srv.config == cfg


async def test_server_database_created(server: AgoraServer) -> None:
    """Given a started server, When checking the database,
    Then the database is connected."""
    assert server.database is not None


async def test_server_plugins_loaded() -> None:
    """Given a config with an enabled plugin, When starting the server,
    Then the plugin is loaded and its tools are available."""
    mod = _make_echo_module()
    monkeypatch_targets: dict[str, types.ModuleType] = {
        "_echo_plugin_module": mod,
    }
    original_import = importlib.import_module

    def _patched_import(name: str) -> types.ModuleType:
        if name in monkeypatch_targets:
            return monkeypatch_targets[name]
        return original_import(name)

    importlib.import_module = _patched_import  # type: ignore[assignment]
    try:
        cfg: dict[str, object] = {
            "db_path": ":memory:",
            "plugins": [
                {
                    "name": "echo",
                    "enabled": True,
                    "module": "_echo_plugin_module",
                    "class_name": "ChatPlugin",
                    "config": {},
                },
            ],
        }
        srv = AgoraServer(config=cfg, skip_transport=True)
        await srv.start()
        assert len(srv.plugins) == 1
        assert srv.plugins[0].name == "echo"
        await srv.stop()
    finally:
        importlib.import_module = original_import


async def test_server_has_eventbus(server: AgoraServer) -> None:
    """Given a started server, When checking the eventbus,
    Then the eventbus is created and accessible."""
    assert server.eventbus is not None
    assert isinstance(server.eventbus, EventBus)


async def test_call_tool_unknown_raises_keyerror(server: AgoraServer) -> None:
    """Given a running server, When calling an unknown tool,
    Then KeyError is raised."""
    with pytest.raises(KeyError, match="TOOL_NOT_FOUND"):
        await server.call_tool("nonexistent_tool", {})


async def test_call_tool_register_with_capabilities(server: AgoraServer) -> None:
    """Given a running server, When registering with capabilities and manifest,
    Then the agent is created with those fields."""
    result = await server.call_tool(
        "register",
        {
            "name": "carol",
            "role": "scout",
            "capabilities": ["code", "review"],
            "manifest": {"team": "core"},
        },
    )
    assert "agent_id" in result
    agent_id = str(result["agent_id"])
    agent = await server.call_tool("get_agent", {"agent_id": agent_id})
    assert agent["agent"] is not None
    assert agent["agent"]["name"] == "carol"  # type: ignore[index]
    assert agent["agent"]["role"] == "scout"  # type: ignore[index]


async def test_call_tool_get_agent(server: AgoraServer) -> None:
    """Given a registered agent, When getting by id,
    Then the agent dict is returned."""
    reg = await server.call_tool("register", {"name": "dave"})
    agent_id = str(reg["agent_id"])
    result = await server.call_tool("get_agent", {"agent_id": agent_id})
    assert result["agent"] is not None
    assert result["agent"]["name"] == "dave"  # type: ignore[index]


async def test_call_tool_get_agent_by_name(server: AgoraServer) -> None:
    """Given a registered agent, When getting by name,
    Then the agent dict is returned."""
    await server.call_tool("register", {"name": "eve"})
    result = await server.call_tool("get_agent_by_name", {"name": "eve"})
    assert result["agent"] is not None
    assert result["agent"]["name"] == "eve"  # type: ignore[index]


async def test_tool_descriptions_visible_via_fastmcp() -> None:
    """Given a server with Chat plugin, When listing MCP tools,
    Then chat tools have non-empty descriptions and backbone tools have empty."""
    cfg: dict[str, object] = {
        "db_path": ":memory:",
        "plugins": [
            {
                "name": "chat",
                "enabled": True,
                "module": "agora.plugins.chat",
                "class_name": "ChatPlugin",
                "config": {},
            },
        ],
    }
    srv = AgoraServer(config=cfg, skip_transport=True)
    await srv.start()
    try:
        assert srv._mcp is not None  # noqa: SLF001
        mcp_tools = await srv._mcp.list_tools(run_middleware=False)  # noqa: SLF001
        tools_by_name = {t.name: t for t in mcp_tools}

        # Chat tools must carry descriptions from ToolDef
        chat_expected = {
            "chat_post_message": "message",
            "chat_read_messages": "read",
            "chat_list_channels": "channel",
            "chat_summarize_channel": "Summarize",
        }
        for tool_name, keyword in chat_expected.items():
            assert tool_name in tools_by_name, f"{tool_name} missing from MCP tools"
            desc = tools_by_name[tool_name].description
            assert desc, f"{tool_name} has empty description"
            assert keyword.lower() in desc.lower(), (
                f"{tool_name} description '{desc}' missing keyword '{keyword}'"
            )

        # Backbone tools exist but have empty descriptions (no ToolDef desc)
        for backbone_tool in ("register", "heartbeat", "list_agents",
                              "get_agent", "get_agent_by_name"):
            assert backbone_tool in tools_by_name, (
                f"{backbone_tool} missing from MCP tools"
            )
            assert tools_by_name[backbone_tool].description == "", (
                f"{backbone_tool} should have empty description"
            )
    finally:
        await srv.stop()
