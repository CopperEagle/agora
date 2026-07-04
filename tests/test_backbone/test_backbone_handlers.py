"""Tests for backbone handler typed parameters and schema generation.

Verifies that the 5 backbone handlers (_handle_register, _handle_heartbeat,
_handle_list_agents, _handle_get_agent, _handle_get_agent_by_name) have
correct typed signatures, generate valid FastMCP schemas, and behave correctly
with optional params and missing data.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agora.backbone.server import AgoraServer, _make_typed_wrapper


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a started AgoraServer with skip_transport=True."""
    srv = AgoraServer(
        config={"db_path": ":memory:", "plugins": []},
        skip_transport=True,
    )
    await srv.start()
    yield srv
    await srv.stop()


# ── Signature tests ──────────────────────────────────────────────


async def test_handle_register_signature() -> None:
    """Given the _handle_register method, When inspecting its signature,
    Then it has name, role, capabilities, manifest params with correct types."""
    sig = inspect.signature(AgoraServer._handle_register)
    params = sig.parameters

    # name: str (required)
    assert "name" in params
    assert params["name"].annotation == "str"
    assert params["name"].default is inspect.Parameter.empty

    # role: str | None = None (optional)
    assert "role" in params
    assert params["role"].default is None

    # capabilities: list[str] | None = None (optional)
    assert "capabilities" in params
    assert params["capabilities"].default is None

    # manifest: dict[str, object] | None = None (optional)
    assert "manifest" in params
    assert params["manifest"].default is None

    # self and **kwargs still present
    assert "self" in params
    assert "kwargs" in params


async def test_handle_heartbeat_signature() -> None:
    """Given the _handle_heartbeat method, When inspecting its signature,
    Then it has agent_id param."""
    sig = inspect.signature(AgoraServer._handle_heartbeat)
    params = sig.parameters

    assert "agent_id" in params
    assert params["agent_id"].annotation == "str"
    assert params["agent_id"].default is inspect.Parameter.empty


async def test_handle_list_agents_signature() -> None:
    """Given the _handle_list_agents method, When inspecting its signature,
    Then it has role and name_prefix params."""
    sig = inspect.signature(AgoraServer._handle_list_agents)
    params = sig.parameters

    # role: str | None = None (optional)
    assert "role" in params
    assert params["role"].default is None

    # name_prefix: str | None = None (optional)
    assert "name_prefix" in params
    assert params["name_prefix"].default is None

    # self and **kwargs still present
    assert "self" in params
    assert "kwargs" in params


async def test_handle_get_agent_signature() -> None:
    """Given the _handle_get_agent method, When inspecting its signature,
    Then it has agent_id param."""
    sig = inspect.signature(AgoraServer._handle_get_agent)
    params = sig.parameters

    assert "agent_id" in params
    assert params["agent_id"].annotation == "str"
    assert params["agent_id"].default is inspect.Parameter.empty


async def test_handle_get_agent_by_name_signature() -> None:
    """Given the _handle_get_agent_by_name method, When inspecting its signature,
    Then it has name param."""
    sig = inspect.signature(AgoraServer._handle_get_agent_by_name)
    params = sig.parameters

    assert "name" in params
    assert params["name"].annotation == "str"
    assert params["name"].default is inspect.Parameter.empty


# ── Schema generation tests ──────────────────────────────────────


async def test_handle_register_schema() -> None:
    """Given the register handler, When creating a FastMCP Tool,
    Then the schema has correct params."""
    from fastmcp.tools.base import Tool

    mock_instance = AsyncMock(spec=AgoraServer)
    mock_instance._registry = AsyncMock()
    mock_instance._registry.register = AsyncMock(return_value="test-id")
    bound_handler = AgoraServer._handle_register.__get__(
        mock_instance, AgoraServer,
    )

    router = AsyncMock()
    router.route = AsyncMock(return_value={"agent_id": "test-id"})
    wrapper = _make_typed_wrapper(router, "register", bound_handler)
    mcp_tool = Tool.from_function(wrapper, name="register")

    params: dict[str, Any] = mcp_tool.parameters  # type: ignore[assignment]
    props: dict[str, Any] = params["properties"]  # type: ignore[assignment]
    required: list[str] = params["required"]  # type: ignore[assignment]

    assert "name" in props
    assert props["name"]["type"] == "string"
    assert "role" in props
    assert "capabilities" in props
    assert "manifest" in props
    assert "name" in required


async def test_handle_heartbeat_schema() -> None:
    """Given the heartbeat handler, When creating a FastMCP Tool,
    Then the schema has agent_id string param."""
    from fastmcp.tools.base import Tool

    mock_instance = AsyncMock(spec=AgoraServer)
    mock_instance._registry = AsyncMock()
    bound_handler = AgoraServer._handle_heartbeat.__get__(
        mock_instance, AgoraServer,
    )

    router = AsyncMock()
    router.route = AsyncMock(return_value={"ok": True})
    wrapper = _make_typed_wrapper(router, "heartbeat", bound_handler)
    mcp_tool = Tool.from_function(wrapper, name="heartbeat")

    params: dict[str, Any] = mcp_tool.parameters  # type: ignore[assignment]
    props: dict[str, Any] = params["properties"]  # type: ignore[assignment]

    assert "agent_id" in props
    assert props["agent_id"]["type"] == "string"


async def test_handle_list_agents_schema() -> None:
    """Given the list_agents handler, When creating a FastMCP Tool,
    Then the schema has role and name_prefix properties."""
    from fastmcp.tools.base import Tool

    mock_instance = AsyncMock(spec=AgoraServer)
    mock_instance._registry = AsyncMock()
    mock_instance._registry.list_agents = AsyncMock(return_value=[])
    bound_handler = AgoraServer._handle_list_agents.__get__(
        mock_instance, AgoraServer,
    )

    router = AsyncMock()
    router.route = AsyncMock(return_value={"agents": []})
    wrapper = _make_typed_wrapper(router, "list_agents", bound_handler)
    mcp_tool = Tool.from_function(wrapper, name="list_agents")

    params: dict[str, Any] = mcp_tool.parameters  # type: ignore[assignment]
    props = params.get("properties", {})
    assert "role" in props
    # Optional params use anyOf: [{type: string}, {type: null}]
    role_schema = props["role"]
    if "anyOf" in role_schema:
        assert any(s.get("type") == "string" for s in role_schema["anyOf"])
    else:
        assert role_schema["type"] == "string"
    assert "name_prefix" in props
    name_prefix_schema = props["name_prefix"]
    if "anyOf" in name_prefix_schema:
        assert any(s.get("type") == "string" for s in name_prefix_schema["anyOf"])
    else:
        assert name_prefix_schema["type"] == "string"
    assert "_agent_id" in props
    assert len(props) == 3  # role, name_prefix, _agent_id


# ── Behavioral tests (via server.call_tool) ──────────────────────


async def test_handle_register_with_role_none(server: AgoraServer) -> None:
    """Given a server, When registering with only name (no role),
    Then registration succeeds."""
    result = await server.call_tool("register", {"name": "test-agent"})
    assert "agent_id" in result
    assert isinstance(result["agent_id"], str)
    assert len(result["agent_id"]) == 36


async def test_handlers_receive_agent_id_via_kwargs(server: AgoraServer) -> None:
    """Given a registered agent, When calling heartbeat with _agent_id in kwargs,
    Then the handler accepts it via **kwargs."""
    reg = await server.call_tool("register", {"name": "kw-agent"})
    agent_id = str(reg["agent_id"])
    # _agent_id is injected by middleware, but call_tool bypasses it;
    # the handler still accepts it via **kwargs
    result = await server.call_tool(
        "heartbeat",
        {"agent_id": agent_id, "_agent_id": agent_id},
    )
    assert result["ok"] is True


async def test_get_agent_with_missing_id_returns_none(server: AgoraServer) -> None:
    """Given no agents, When getting an agent with non-existent ID,
    Then agent is None."""
    result = await server.call_tool(
        "get_agent",
        {"agent_id": "non-existent-id"},
    )
    assert result["agent"] is None


async def test_get_agent_by_name_with_missing_name_returns_none(
    server: AgoraServer,
) -> None:
    """Given no agents, When getting an agent by non-existent name,
    Then agent is None."""
    result = await server.call_tool(
        "get_agent_by_name",
        {"name": "non-existent-name"},
    )
    assert result["agent"] is None


async def test_register_then_get_by_name(server: AgoraServer) -> None:
    """Given a registered agent, When getting by name,
    Then the agent is found."""
    await server.call_tool("register", {"name": "findme"})
    result = await server.call_tool("get_agent_by_name", {"name": "findme"})
    assert result["agent"] is not None
    assert result["agent"]["name"] == "findme"  # type: ignore[index]


async def test_list_agents_after_register(server: AgoraServer) -> None:
    """Given multiple registered agents, When listing agents,
    Then all are returned."""
    await server.call_tool("register", {"name": "agent-a"})
    await server.call_tool("register", {"name": "agent-b"})
    result = await server.call_tool("list_agents", {})
    assert len(result["agents"]) == 2  # type: ignore[arg-type]
