"""End-to-end agent usability integration test.

Simulates a first-time agent connecting to Agora and using ALL 8 tools
entirely through ``tools/list`` schemas.  This exercises the full pipeline:

  MCP schema generation -> auth -> dispatch -> error handling

The test is structured as a single comprehensive function with helper
functions for readability.  It validates:

  1. Schema introspection via ``server._mcp.list_tools()``
  2. Agent registration and heartbeat
  3. Chat: post, read, list channels, summarize
  4. Agent discovery: list, get_by_id, get_by_name
  5. Auth enforcement (NOT_AUTHORIZED on missing _agent_id)
  6. Unknown tool dispatch (TOOL_NOT_FOUND with available tools list)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agora.backbone.server import AgoraServer

# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a started AgoraServer with Chat plugin, in-memory DB."""
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
    yield srv
    await srv.stop()


# ── Helpers ─────────────────────────────────────────────────────

ALL_8_TOOLS = [
    "register",
    "list_agents",
    "get_agent",
    "get_agent_by_name",
    "chat_post_message",
    "chat_read_messages",
    "chat_list_channels",
    "chat_summarize_channel",
]


async def _get_mcp_schemas(
    server: AgoraServer,
) -> dict[str, dict[str, Any]]:
    """Return tool name -> JSON Schema parameters from ``tools/list``."""
    assert server._mcp is not None  # noqa: SLF001
    mcp_tools = await server._mcp.list_tools(run_middleware=False)  # noqa: SLF001
    return {t.name: dict(t.parameters) for t in mcp_tools}


def _assert_tool_present(
    schemas: dict[str, dict[str, Any]],
    tool_name: str,
    *,
    required_fields: list[str] | None = None,
) -> None:
    """Assert a tool exists in schemas with type=object and optional required."""
    assert tool_name in schemas, f"{tool_name} missing from tools/list"
    params = schemas[tool_name]
    assert params["type"] == "object", (
        f"{tool_name} schema type is {params.get('type')!r}"
    )
    if required_fields:
        props: dict[str, Any] = params.get("properties", {})
        req: list[str] = params.get("required", [])
        for field in required_fields:
            assert field in props, f"{tool_name} missing property '{field}'"
            assert props[field]["type"] == "string", (
                f"{tool_name}.{field} type is {props[field].get('type')!r}"
            )
            assert field in req, f"{tool_name} missing '{field}' in required"


def _assert_schemas_valid(schemas: dict[str, dict[str, Any]]) -> None:
    """Assert all 8 tools have valid type=object schemas."""
    for name in ALL_8_TOOLS:
        assert name in schemas, f"{name} missing from tools/list"

    for tool_name, params in schemas.items():
        assert params["type"] == "object", (
            f"{tool_name} schema type is {params.get('type')!r}"
        )
        # list_agents has no typed params -- acceptable
        if tool_name != "list_agents":
            assert params.get("properties"), (
                f"{tool_name} has empty/missing properties"
            )


async def _test_schema_introspection(server: AgoraServer) -> None:
    """Phase 1: Inspect MCP schemas for all 8 tools."""
    schemas = await _get_mcp_schemas(server)
    _assert_schemas_valid(schemas)

    _assert_tool_present(schemas, "register", required_fields=["name"])
    _assert_tool_present(
        schemas, "chat_post_message",
        required_fields=["channel", "content"],
    )
    _assert_tool_present(schemas, "chat_read_messages", required_fields=["channel"])
    _assert_tool_present(schemas, "get_agent", required_fields=["agent_id"])
    _assert_tool_present(schemas, "get_agent_by_name", required_fields=["name"])
    _assert_tool_present(schemas, "chat_summarize_channel", required_fields=["channel"])

    # list_agents: type=object, no required params
    list_agents_schema = schemas["list_agents"]
    assert list_agents_schema["type"] == "object"

    # list_channels: optional prefix property
    lc_schema = schemas["chat_list_channels"]
    assert "prefix" in lc_schema.get("properties", {})


async def _test_agent_registration(server: AgoraServer) -> str:
    """Phase 2: Register agent, return agent_id."""
    reg_result = await server.call_tool("register", {"name": "test-agent"})
    assert "agent_id" in reg_result
    agent_id: str = str(reg_result["agent_id"])
    assert len(agent_id) == 36  # UUID format
    return agent_id


async def _test_chat_operations(
    server: AgoraServer,
    agent_id: str,
) -> None:
    """Phase 3: Post, read, list channels."""
    post_result = await server.call_tool(
        "chat_post_message",
        {"channel": "#test", "content": "hello", "_agent_id": agent_id},
    )
    assert "message_id" in post_result
    assert len(str(post_result["message_id"])) == 36

    read_result = await server.call_tool(
        "chat_read_messages",
        {"channel": "#test"},
    )
    messages: list[dict[str, Any]] = read_result["messages"]  # type: ignore[assignment]
    assert len(messages) >= 1
    assert messages[0]["content"] == "hello"

    channels_result = await server.call_tool("chat_list_channels", {})
    channels: list[dict[str, Any]] = channels_result["channels"]  # type: ignore[assignment]
    channel_names = [c["name"] for c in channels]
    assert "#test" in channel_names


async def _test_agent_management(
    server: AgoraServer,
    agent_id: str,
) -> None:
    """Phase 4: List agents, get by id, get by name."""
    list_result = await server.call_tool("list_agents", {})
    agents: list[dict[str, Any]] = list_result["agents"]  # type: ignore[assignment]
    agent_ids = [a["id"] for a in agents]
    assert agent_id in agent_ids

    get_result = await server.call_tool(
        "get_agent",
        {"agent_id": agent_id},
    )
    agent: dict[str, Any] | None = get_result.get("agent")  # type: ignore[assignment]
    assert agent is not None
    assert agent["id"] == agent_id

    gbn_result = await server.call_tool(
        "get_agent_by_name",
        {"name": "test-agent"},
    )
    agent2: dict[str, Any] | None = gbn_result.get("agent")  # type: ignore[assignment]
    assert agent2 is not None
    assert agent2["id"] == agent_id


async def _test_channel_summary(server: AgoraServer) -> None:
    """Phase 5: Summarize channel with messages."""
    summary = await server.call_tool(
        "chat_summarize_channel",
        {"channel": "#test"},
    )
    assert "message_count" in summary
    assert summary["message_count"] >= 1
    assert "participants" in summary
    assert summary["participants"] >= 1
    assert "summary" in summary


async def _test_error_paths(server: AgoraServer) -> None:
    """Phase 6: NOT_AUTHORIZED and TOOL_NOT_FOUND error handling."""
    # NOT_AUTHORIZED: calling a protected tool without _agent_id via router
    assert server._router is not None  # noqa: SLF001
    auth_error = await server._router.route(  # noqa: SLF001
        "chat_post_message",
        {"channel": "#test", "content": "no auth"},
        session_id=None,
    )
    assert auth_error["error"] == "NOT_AUTHORIZED"
    assert "fix" in auth_error
    assert "register" in auth_error["fix"].lower()

    # TOOL_NOT_FOUND: calling an unknown tool via call_tool
    not_found = await server.call_tool("unknown_tool", {})
    assert not_found["error"] == "TOOL_NOT_FOUND"
    assert "fix" in not_found
    assert "Available" in not_found["fix"] or "Check tool name" in not_found["fix"]


# ── Comprehensive end-to-end test ──────────────────────────────


async def test_full_agent_lifecycle_from_tools_list_schemas(
    server: AgoraServer,
) -> None:
    """Simulate a first-time agent using all 8 tools via MCP schemas.

    This is ONE test that walks through the entire agent lifecycle:

    Phase 1 - Schema introspection (tools/list)
    Phase 2 - Agent registration
    Phase 3 - Chat: post -> read -> list channels
    Phase 4 - Agent management: list, get by id/name
    Phase 5 - Channel summary
    Phase 6 - Error paths: NOT_AUTHORIZED, TOOL_NOT_FOUND
    """
    await _test_schema_introspection(server)
    agent_id = await _test_agent_registration(server)
    await _test_chat_operations(server, agent_id)
    await _test_agent_management(server, agent_id)
    await _test_channel_summary(server)
    await _test_error_paths(server)
