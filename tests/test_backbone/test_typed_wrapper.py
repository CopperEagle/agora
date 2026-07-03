"""Tests for _make_typed_wrapper — typed wrapper for MCP schema generation."""

from __future__ import annotations

import inspect

from unittest.mock import AsyncMock

import pytest

from agora.backbone.server import _make_typed_wrapper


# ── Test 1: signature preservation ─────────────────────────────────


async def test_wrapper_signature_matches_handler() -> None:
    """Given a handler with typed params, When wrapping, Then the wrapper's
    signature has the same params (excluding self, _agent_id)."""

    async def handler(
        channel: str, content: str, parent_id: str | None = None,
    ) -> dict[str, object]:
        """Test handler.

        Args:
            channel: The channel name.
            content: Message content.
            parent_id: Optional parent message ID.
        """
        return {"ok": True}

    router: AsyncMock = AsyncMock()
    router.route = AsyncMock(return_value={"ok": True})

    wrapper = _make_typed_wrapper(router, "test_tool", handler)

    sig = inspect.signature(wrapper)
    params = list(sig.parameters.keys())
    assert params == ["channel", "content", "parent_id"]


# ── Test 2: excludes self and _agent_id ────────────────────────────


async def test_wrapper_excludes_self_and_agent_id() -> None:
    """Given a handler with self and _agent_id params, When wrapping, Then
    these are excluded from the wrapper signature."""

    async def handler(
        self: object, channel: str, _agent_id: str, content: str,
    ) -> dict[str, object]:
        return {"ok": True}

    router: AsyncMock = AsyncMock()
    router.route = AsyncMock(return_value={"ok": True})

    wrapper = _make_typed_wrapper(router, "test_tool", handler)

    sig = inspect.signature(wrapper)
    params = list(sig.parameters.keys())
    assert "self" not in params
    assert "_agent_id" not in params
    assert params == ["channel", "content"]


# ── Test 3: FastMCP schema generation ──────────────────────────────


async def test_schema_from_wrapper_has_correct_params() -> None:
    """Given a typed wrapper, When creating a FastMCP Tool, Then the schema
    has correct properties and required fields."""

    async def handler(
        channel: str, content: str, parent_id: str | None = None,
    ) -> dict[str, object]:
        return {"ok": True}

    router: AsyncMock = AsyncMock()
    router.route = AsyncMock(return_value={"ok": True})

    wrapper = _make_typed_wrapper(router, "test_tool", handler)

    from fastmcp.tools.base import Tool

    mcp_tool = Tool.from_function(wrapper, name="test_tool")

    params: dict[str, object] = mcp_tool.parameters  # type: ignore[assignment]
    assert params["type"] == "object"
    props: dict[str, dict[str, object]] = params["properties"]  # type: ignore[assignment]
    assert "channel" in props
    assert "content" in props
    assert "parent_id" in props
    assert props["channel"]["type"] == "string"
    assert props["content"]["type"] == "string"
    required: list[str] = params["required"]  # type: ignore[assignment]
    assert "channel" in required
    assert "content" in required


# ── Test 4: schema excludes _agent_id ──────────────────────────────


async def test_schema_excludes_agent_id() -> None:
    """Given a handler with _agent_id param, When creating schema, Then
    _agent_id is NOT in the schema."""

    async def handler(
        channel: str, _agent_id: str, content: str,
    ) -> dict[str, object]:
        return {"ok": True}

    router: AsyncMock = AsyncMock()
    router.route = AsyncMock(return_value={"ok": True})

    wrapper = _make_typed_wrapper(router, "test_tool", handler)

    from fastmcp.tools.base import Tool

    mcp_tool = Tool.from_function(wrapper, name="test_tool")

    props: dict[str, dict[str, object]] = mcp_tool.parameters["properties"]  # type: ignore[index]
    assert "_agent_id" not in props


# ── Test 5: wrapper delegates to router.route ──────────────────────


async def test_wrapper_calls_router_route() -> None:
    """Given a wrapped handler, When calling the wrapper, Then router.route()
    is called with correct args."""

    async def handler(channel: str, content: str) -> dict[str, object]:
        return {"ok": True}

    router: AsyncMock = AsyncMock()
    router.route = AsyncMock(return_value={"ok": True})

    wrapper = _make_typed_wrapper(router, "test_tool", handler)

    result = await wrapper(channel="#test", content="hello")

    router.route.assert_called_once_with(
        "test_tool",
        {"channel": "#test", "content": "hello"},
        session_id=None,
    )
    assert result == {"ok": True}


# ── Test 6: all required params ────────────────────────────────────


async def test_handler_with_all_required_params() -> None:
    """Given a handler with no optional params, When creating schema, Then
    all params are in required."""

    async def handler(name: str, role: str) -> dict[str, object]:
        return {"ok": True}

    router: AsyncMock = AsyncMock()
    router.route = AsyncMock(return_value={"ok": True})

    wrapper = _make_typed_wrapper(router, "test_tool", handler)

    from fastmcp.tools.base import Tool

    mcp_tool = Tool.from_function(wrapper, name="test_tool")

    required: list[str] = mcp_tool.parameters["required"]  # type: ignore[index]
    assert required == ["name", "role"]
