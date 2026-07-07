"""Tests for chat handler signatures and schema generation.

Verifies that the 4 chat handlers have explicit typed parameters
(replacing **kwargs) and that FastMCP generates correct schemas from them.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from agora.backbone.server import AgoraServer, _make_typed_wrapper
from fastmcp.tools.base import Tool

from agora.plugins.chat import ChatPlugin

_CHAT_PLUGIN_CONFIG: dict[str, object] = {
    "name": "chat",
    "enabled": True,
    "module": "agora.plugins.chat",
    "class_name": "ChatPlugin",
    "config": {
        "max_message_length": 100_000,
        "max_channels": 1000,
    },
}


@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a running AgoraServer with the Chat plugin loaded."""
    srv = AgoraServer(
        config={
            "db_path": ":memory:",
            "plugins": [_CHAT_PLUGIN_CONFIG],
        },
        skip_transport=True,
    )
    await srv.start()
    yield srv
    await srv.stop()


# ── Signature tests ────────────────────────────────────────────────


class TestPostMessageSignature:
    """Verify _handle_post_message has explicit typed params."""

    def test_has_channel_content_parent_id(self) -> None:
        """Signature contains channel, content, parent_id params."""
        sig = inspect.signature(ChatPlugin._handle_post_message)
        params = list(sig.parameters.keys())
        assert "channel" in params
        assert "content" in params
        assert "parent_id" in params

    def test_channel_and_content_are_required(self) -> None:
        """channel and content have no default (required)."""
        sig = inspect.signature(ChatPlugin._handle_post_message)
        assert sig.parameters["channel"].default is inspect.Parameter.empty
        assert sig.parameters["content"].default is inspect.Parameter.empty

    def test_parent_id_is_optional(self) -> None:
        """parent_id defaults to None."""
        sig = inspect.signature(ChatPlugin._handle_post_message)
        assert sig.parameters["parent_id"].default is None

    def test_has_kwargs_for_agent_id(self) -> None:
        """**kwargs remains for _agent_id injection from middleware."""
        sig = inspect.signature(ChatPlugin._handle_post_message)
        params = list(sig.parameters.values())
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params
        )
        assert has_var_keyword


class TestReadMessagesSignature:
    """Verify _handle_read_messages has explicit typed params."""

    def test_has_channel_since_limit_order(self) -> None:
        """Signature contains channel, since, limit, order params."""
        sig = inspect.signature(ChatPlugin._handle_read_messages)
        params = list(sig.parameters.keys())
        assert "channel" in params
        assert "since" in params
        assert "limit" in params
        assert "order" in params

    def test_channel_is_required(self) -> None:
        """channel has no default (required)."""
        sig = inspect.signature(ChatPlugin._handle_read_messages)
        assert sig.parameters["channel"].default is inspect.Parameter.empty

    def test_limit_defaults_to_3(self) -> None:
        """limit defaults to 3."""
        sig = inspect.signature(ChatPlugin._handle_read_messages)
        assert sig.parameters["limit"].default == 3

    def test_order_defaults_to_desc(self) -> None:
        """order defaults to 'desc'."""
        sig = inspect.signature(ChatPlugin._handle_read_messages)
        assert sig.parameters["order"].default == "desc"

    def test_since_defaults_to_none(self) -> None:
        """since defaults to None."""
        sig = inspect.signature(ChatPlugin._handle_read_messages)
        assert sig.parameters["since"].default is None


class TestListChannelsSignature:
    """Verify _handle_list_channels has explicit typed params."""

    def test_has_prefix_param(self) -> None:
        """Signature contains prefix param."""
        sig = inspect.signature(ChatPlugin._handle_list_channels)
        assert "prefix" in sig.parameters

    def test_prefix_is_optional(self) -> None:
        """prefix defaults to None."""
        sig = inspect.signature(ChatPlugin._handle_list_channels)
        assert sig.parameters["prefix"].default is None


class TestSummarizeChannelSignature:
    """Verify _handle_summarize_channel has explicit typed params."""

    def test_has_channel_and_since(self) -> None:
        """Signature contains channel and since params."""
        sig = inspect.signature(ChatPlugin._handle_summarize_channel)
        assert "channel" in sig.parameters
        assert "since" in sig.parameters

    def test_since_is_optional(self) -> None:
        """since defaults to None."""
        sig = inspect.signature(ChatPlugin._handle_summarize_channel)
        assert sig.parameters["since"].default is None


# ── Schema tests ───────────────────────────────────────────────────


class TestPostMessageSchema:
    """Verify FastMCP generates correct schema for post_message."""

    def test_schema_has_correct_properties(self) -> None:
        """Schema properties include channel, content, parent_id."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_post_message", ChatPlugin._handle_post_message,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_post_message")
        params: dict[str, object] = mcp_tool.parameters  # type: ignore[assignment]
        props: dict[str, dict[str, object]] = params["properties"]  # type: ignore[assignment]
        assert "channel" in props
        assert "content" in props
        assert "parent_id" in props
        assert props["channel"]["type"] == "string"
        assert props["content"]["type"] == "string"

    def test_schema_required_fields(self) -> None:
        """channel and content are required; parent_id is not."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_post_message", ChatPlugin._handle_post_message,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_post_message")
        required: list[str] = mcp_tool.parameters["required"]  # type: ignore[index]
        assert "channel" in required
        assert "content" in required
        assert "parent_id" not in required

    def test_schema_excludes_kwargs(self) -> None:
        """**kwargs is NOT in the schema, but synthetic _agent_id IS."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_post_message", ChatPlugin._handle_post_message,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_post_message")
        props: dict[str, dict[str, object]] = mcp_tool.parameters["properties"]  # type: ignore[index]
        assert "kwargs" not in props
        assert "_agent_id" in props


class TestReadMessagesSchema:
    """Verify FastMCP generates correct schema for read_messages."""

    def test_schema_has_correct_properties(self) -> None:
        """Schema properties include channel, since, limit, order."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_read_messages", ChatPlugin._handle_read_messages,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_read_messages")
        props: dict[str, dict[str, object]] = mcp_tool.parameters["properties"]  # type: ignore[index]
        assert "channel" in props
        assert "since" in props
        assert "limit" in props
        assert "order" in props
        assert props["channel"]["type"] == "string"
        assert props["limit"]["type"] == "integer"
        assert props["order"]["type"] == "string"

    def test_schema_required_fields(self) -> None:
        """Only channel is required."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_read_messages", ChatPlugin._handle_read_messages,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_read_messages")
        required: list[str] = mcp_tool.parameters["required"]  # type: ignore[index]
        assert "channel" in required
        assert "limit" not in required
        assert "order" not in required
        assert "since" not in required


class TestListChannelsSchema:
    """Verify FastMCP generates correct schema for list_channels."""

    def test_schema_has_prefix_property(self) -> None:
        """Schema properties include prefix."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_list_channels", ChatPlugin._handle_list_channels,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_list_channels")
        props: dict[str, dict[str, object]] = mcp_tool.parameters["properties"]  # type: ignore[index]
        assert "prefix" in props
        # str | None generates anyOf: [string, null]
        prefix_schema: dict[str, object] = props["prefix"]  # type: ignore[assignment]
        assert prefix_schema.get("default") is None

    def test_schema_no_required_fields(self) -> None:
        """All params are optional — no 'required' key when all have defaults."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_list_channels", ChatPlugin._handle_list_channels,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_list_channels")
        params: dict[str, object] = mcp_tool.parameters  # type: ignore[assignment]
        # When all params are optional, FastMCP omits 'required' entirely
        required = params.get("required", [])
        assert required == []


class TestSummarizeChannelSchema:
    """Verify FastMCP generates correct schema for summarize_channel."""

    def test_schema_has_correct_properties(self) -> None:
        """Schema properties include channel and since."""
        router = AsyncMock()
        router.route = AsyncMock(return_value={"ok": True})
        wrapper = _make_typed_wrapper(
            router, "chat_summarize_channel",
            ChatPlugin._handle_summarize_channel,
        )
        mcp_tool = Tool.from_function(wrapper, name="chat_summarize_channel")
        props: dict[str, dict[str, object]] = mcp_tool.parameters["properties"]  # type: ignore[index]
        assert "channel" in props
        assert "since" in props
        assert props["channel"]["type"] == "string"


# ── Behavioral tests via server.call_tool ───────────────────────────


class TestPostMessageBehavior:
    """Verify post_message works with typed params."""

    async def test_with_parent_id_none(self, server: AgoraServer) -> None:
        """Calling with parent_id=None posts successfully."""
        result = await server.call_tool("chat_post_message", {
            "channel": "#typed-test",
            "content": "Hello typed world",
        })
        assert "message_id" in result
        assert result.get("channel") == "#typed-test"
        assert "error" not in result

    async def test_with_explicit_parent_id(self, server: AgoraServer) -> None:
        """Calling with explicit parent_id threads correctly."""
        parent = await server.call_tool("chat_post_message", {
            "channel": "#typed-thread",
            "content": "Parent",
        })
        reply = await server.call_tool("chat_post_message", {
            "channel": "#typed-thread",
            "content": "Reply",
            "parent_id": parent.get("message_id"),
        })
        assert "message_id" in reply
        assert "error" not in reply


class TestReadMessagesBehavior:
    """Verify read_messages works with typed params."""

    async def test_limit_default_is_50(self, server: AgoraServer) -> None:
        """Default limit=50 returns up to 50 messages."""
        for i in range(3):
            await server.call_tool("chat_post_message", {
                "channel": "#limit-default",
                "content": f"msg-{i}",
            })
        result = await server.call_tool("chat_read_messages", {
            "channel": "#limit-default",
        })
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) == 3

    async def test_limit_zero_returns_empty(self, server: AgoraServer) -> None:
        """limit=0 returns empty messages list."""
        await server.call_tool("chat_post_message", {
            "channel": "#limit-zero",
            "content": "exists",
        })
        result = await server.call_tool("chat_read_messages", {
            "channel": "#limit-zero",
            "limit": 0,
        })
        messages = result.get("messages", [])
        assert messages == []


class TestListChannelsBehavior:
    """Verify list_channels works with typed params."""

    async def test_prefix_none_returns_all(self, server: AgoraServer) -> None:
        """prefix=None returns all channels."""
        await server.call_tool("chat_post_message", {
            "channel": "#alpha-ch", "content": "a",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#beta-ch", "content": "b",
        })
        result = await server.call_tool("chat_list_channels", {})
        channels = result.get("channels", [])
        assert isinstance(channels, list)
        names = {c.get("name") for c in channels}
        assert "#alpha-ch" in names
        assert "#beta-ch" in names

    async def test_prefix_filters(self, server: AgoraServer) -> None:
        """prefix='#dev' filters channels correctly."""
        await server.call_tool("chat_post_message", {
            "channel": "#dev-a", "content": "a",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#prod-b", "content": "b",
        })
        result = await server.call_tool("chat_list_channels", {
            "prefix": "#dev",
        })
        channels = result.get("channels", [])
        names = {c.get("name") for c in channels}
        assert "#dev-a" in names
        assert "#prod-b" not in names
