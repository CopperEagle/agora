"""Tests for MCP tools/list schema correctness.

Verifies that _register_tools_with_mcp() uses _make_typed_wrapper to produce
correct JSON Schema inputSchema for all tools, including both backbone and
plugin tools.  These tests guard against the old broken pattern where tools
received ``{"type": "object"}`` without detailed properties.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agora.backbone.server import AgoraServer

# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a started AgoraServer with Chat plugin loaded."""
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


async def _get_tools_by_name(
    server: AgoraServer,
) -> dict[str, dict[str, Any]]:
    """Return a dict mapping tool name → its parameters dict (JSON Schema)."""
    assert server._mcp is not None  # noqa: SLF001
    mcp_tools = await server._mcp.list_tools(run_middleware=False)  # noqa: SLF001
    return {t.name: dict(t.parameters) for t in mcp_tools}


# ── Tests ───────────────────────────────────────────────────────


async def test_chat_post_message_schema(server: AgoraServer) -> None:
    """chat_post_message must have channel and content as required strings."""
    tools = await _get_tools_by_name(server)
    params = tools["chat_post_message"]

    props: dict[str, Any] = params["properties"]
    required: list[str] = params["required"]

    assert "channel" in props
    assert props["channel"]["type"] == "string"
    assert "content" in props
    assert props["content"]["type"] == "string"
    assert "channel" in required
    assert "content" in required


async def test_register_schema(server: AgoraServer) -> None:
    """register must have name as required string."""
    tools = await _get_tools_by_name(server)
    params = tools["register"]

    props: dict[str, Any] = params["properties"]
    required: list[str] = params["required"]

    assert "name" in props
    assert props["name"]["type"] == "string"
    assert "name" in required


async def test_all_tools_have_object_schema_with_properties(
    server: AgoraServer,
) -> None:
    """All 8 tools must have inputSchema with type=object and non-empty properties."""
    expected_tools = [
        "register",
        "list_agents",
        "get_agent",
        "get_agent_by_name",
        "chat_post_message",
        "chat_read_messages",
        "chat_list_channels",
        "chat_summarize_channel",
    ]
    tools = await _get_tools_by_name(server)

    for name in expected_tools:
        assert name in tools, f"{name} not found in MCP tools"
        params = tools[name]
        assert params["type"] == "object", (
            f"{name} schema type is {params.get('type')!r}, expected 'object'"
        )
        # Properties must be a non-empty dict (except list_agents which has
        # no typed params — that's fine)
        if name != "list_agents":
            assert params.get("properties"), (
                f"{name} schema has empty/missing properties"
            )


async def test_no_tool_has_bare_object_schema(server: AgoraServer) -> None:
    """No tool should have the old broken ``{"type": "object"}`` without properties."""
    tools = await _get_tools_by_name(server)

    for name, params in tools.items():
        # A tool with only `{"type": "object"}` and no properties is broken
        has_properties = bool(params.get("properties"))
        if not has_properties:
            # list_agents is acceptable — it has no typed params
            assert name == "list_agents", (
                f"{name} has bare schema without properties (old broken pattern)"
            )
