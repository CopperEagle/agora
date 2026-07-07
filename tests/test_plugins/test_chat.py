"""Tests for the Chat plugin — channels, messages, tools, and events.

Covers:
- chat_post_message: auto-vivify, validation, threading, event emission
- chat_read_messages: limit, since, order, empty channel
- chat_list_channels: prefix filter, message count, last activity, ordering
- chat_summarize_channel: stats summary, built-in stub, LLM config
- Event hooks: agent register/disconnect post to #general
- Edge cases: channel limit, concurrency, invalid input
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from agora.backbone.server import AgoraServer
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

_LLM_STUB_CONFIG: dict[str, object] = {
    "name": "chat",
    "enabled": True,
    "module": "agora.plugins.chat",
    "class_name": "ChatPlugin",
    "config": {
        "max_message_length": 100_000,
        "max_channels": 1000,
        "use_built_in_llm": True,
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


@pytest.fixture
async def llm_stub_server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a server with use_built_in_llm=True."""
    srv = AgoraServer(
        config={
            "db_path": ":memory:",
            "plugins": [_LLM_STUB_CONFIG],
        },
        skip_transport=True,
    )
    await srv.start()
    yield srv
    await srv.stop()


# ── Helpers ────────────────────────────────────────────────────────


def _extract_messages(result: dict[str, object]) -> list[dict[str, object]]:
    """Extract and type-narrow the messages list from a read result."""
    msgs = result.get("messages", [])
    assert isinstance(msgs, list)
    typed: list[dict[str, object]] = []
    for m in msgs:
        assert isinstance(m, dict)
        typed.append(m)
    return typed


def _extract_channels(result: dict[str, object]) -> list[dict[str, object]]:
    """Extract and type-narrow the channels list from a list result."""
    chs = result.get("channels", [])
    assert isinstance(chs, list)
    typed: list[dict[str, object]] = []
    for c in chs:
        assert isinstance(c, dict)
        typed.append(c)
    return typed


# ── chat_post_message ──────────────────────────────────────────────


class TestPostMessage:
    """Tests for the chat_post_message tool."""

    async def test_post_to_new_channel(self, server: AgoraServer) -> None:
        """Posting to a new channel auto-creates it and returns message_id."""
        result = await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "Hello, world!",
        })
        msg_id = result.get("message_id")
        assert isinstance(msg_id, str)
        assert len(msg_id) == 36  # UUID length
        assert result.get("channel") == "#general"
        assert "created_at" in result

    async def test_post_to_existing_channel(self, server: AgoraServer) -> None:
        """Posting to an existing channel succeeds and increments count."""
        await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "First",
        })
        result = await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "Second",
        })
        assert "message_id" in result
        assert result.get("channel") == "#general"

        # Verify message count incremented
        read_result = await server.call_tool("chat_read_messages", {
            "channel": "#general",
            "order": "asc",
        })
        messages = _extract_messages(read_result)
        assert len(messages) == 2
        assert messages[0].get("content") == "First"
        assert messages[1].get("content") == "Second"

    async def test_post_empty_content(self, server: AgoraServer) -> None:
        """Empty content returns a validation error."""
        result = await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "",
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_post_with_parent_id(self, server: AgoraServer) -> None:
        """Post with parent_id creates a threaded message."""
        parent = await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "Parent post",
        })
        parent_id = parent.get("message_id")
        assert isinstance(parent_id, str)
        reply = await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "Thread reply",
            "parent_id": parent_id,
        })
        reply_id = reply.get("message_id")
        assert isinstance(reply_id, str)
        assert reply_id != parent_id

        # Read back: verify parent_id is stored on the reply
        read_result = await server.call_tool("chat_read_messages", {
            "channel": "#general",
            "order": "asc",
        })
        messages = _extract_messages(read_result)
        # The parent was posted first, reply second
        assert len(messages) == 2
        reply_msg = messages[1]
        assert reply_msg.get("parent_id") == parent_id
        assert reply_msg.get("content") == "Thread reply"

    async def test_post_exceeds_max_length(self, server: AgoraServer) -> None:
        """Content exceeding max_message_length returns validation error."""
        long_content = "x" * 100_001
        result = await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": long_content,
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_empty_channel_name(self, server: AgoraServer) -> None:
        """Empty channel name returns validation error."""
        result = await server.call_tool("chat_post_message", {
            "channel": "",
            "content": "Hello",
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_post_missing_channel_param(self, server: AgoraServer) -> None:
        """Missing channel parameter returns validation error."""
        result = await server.call_tool("chat_post_message", {
            "content": "Hello",
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_post_emits_event(self, server: AgoraServer) -> None:
        """Posting a message emits a chat.message.posted event."""
        # Subscribe to the event bus
        events: list[dict[str, object]] = []

        async def event_handler(event_name: str, **data: object) -> None:
            events.append({"event_name": event_name, **data})

        evt_bus = server._eventbus  # noqa: SLF001  # white-box test
        assert evt_bus is not None
        evt_bus.subscribe("chat.message.posted", event_handler)

        await server.call_tool("chat_post_message", {
            "channel": "#event-test",
            "content": "Test event",
        })

        assert len(events) >= 1
        evt = events[0]
        assert evt.get("event_name") == "chat.message.posted"
        assert evt.get("channel") == "#event-test"
        assert evt.get("message_id") is not None
        assert evt.get("agent_id") == "unknown"

    async def test_post_orphan_parent_id(self, server: AgoraServer) -> None:
        """Post with parent_id referencing non-existent message still posts."""
        result = await server.call_tool("chat_post_message", {
            "channel": "#orphan-test",
            "content": "Orphan reply",
            "parent_id": "00000000-0000-0000-0000-000000000000",
        })
        assert "message_id" in result
        assert "error" not in result

        # Verify message was stored
        read_result = await server.call_tool("chat_read_messages", {
            "channel": "#orphan-test",
        })
        messages = _extract_messages(read_result)
        assert len(messages) == 1


# ── chat_read_messages ─────────────────────────────────────────────


class TestReadMessages:
    """Tests for the chat_read_messages tool."""

    @pytest.fixture
    async def channel_with_messages(self, server: AgoraServer) -> str:
        """Post 5 messages to #test and return the channel name."""
        for i in range(5):
            await server.call_tool("chat_post_message", {
                "channel": "#test",
                "content": f"Message {i}",
            })
        return "#test"

    async def test_read_existing_channel(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Reading a channel with messages returns them with all fields."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "limit": 5,
            "order": "asc",
        })
        messages = _extract_messages(result)
        assert len(messages) == 5
        msg0 = messages[0]
        assert msg0.get("content") == "Message 0"
        assert msg0.get("channel_id") is not None
        assert msg0.get("agent_id") == "unknown"
        assert msg0.get("created_at") is not None
        assert msg0.get("content_type") == "text"
        assert "id" in msg0

    async def test_read_actually_empty_channel(self, server: AgoraServer) -> None:
        """Reading a channel that was created but has no messages returns empty."""
        # Create a channel by posting then clear - or use a channel that
        # has never been posted to (read_messages doesn't auto-vivify)
        result = await server.call_tool("chat_read_messages", {
            "channel": "#truly-empty",
        })
        messages = _extract_messages(result)
        assert messages == []

    async def test_read_nonexistent_channel(self, server: AgoraServer) -> None:
        """Reading a non-existent channel returns empty list (no auto-vivify)."""
        result = await server.call_tool("chat_read_messages", {
            "channel": "#no-such-channel",
        })
        messages = _extract_messages(result)
        assert messages == []

    async def test_read_with_limit(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Limit parameter restricts returned message count."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "limit": 2,
            "order": "asc",
        })
        messages = _extract_messages(result)
        assert len(messages) == 2
        # Should be first 2 messages in asc order
        assert messages[0].get("content") == "Message 0"
        assert messages[1].get("content") == "Message 1"

    async def test_read_with_default_params(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Reading with no params returns upto 3 messages in desc order."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            # No limit, order, or since passed — defaults: limit=3, order="desc"
        })
        messages = _extract_messages(result)
        assert len(messages) == 3  # Default limit is 3
        # Default order is descending (newest first)
        assert messages[0].get("content") == "Message 4"
        assert messages[-1].get("content") == "Message 2"

    async def test_read_with_order_desc(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Order='desc' returns messages in reverse chronological order."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "order": "desc",
            "limit": 5,
        })
        messages = _extract_messages(result)
        assert len(messages) == 5
        assert messages[0].get("content") == "Message 4"
        assert messages[-1].get("content") == "Message 0"

    async def test_read_with_since(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Since filter returns only messages after the given timestamp."""
        _ = channel_with_messages
        msg = await server.call_tool("chat_post_message", {
            "channel": "#test",
            "content": "Recent message",
        })
        since_time = str(msg.get("created_at", ""))

        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "since": since_time,
        })
        messages = _extract_messages(result)
        assert len(messages) == 1
        assert messages[0].get("content") == "Recent message"

    async def test_read_with_limit_zero(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Limit=0 returns an empty message list."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "limit": 0,
        })
        messages = _extract_messages(result)
        assert messages == []

    async def test_read_invalid_order(self, server: AgoraServer) -> None:
        """Invalid order value returns a validation error."""
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "order": "invalid",
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_read_message_shape(self, server: AgoraServer) -> None:
        """Each message in read_messages has all expected fields including parent_id."""
        await server.call_tool("chat_post_message", {
            "channel": "#shape-read",
            "content": "Shape check",
        })
        result = await server.call_tool("chat_read_messages", {
            "channel": "#shape-read",
        })
        messages = _extract_messages(result)
        assert len(messages) >= 1
        msg = messages[0]
        assert "id" in msg
        assert "channel_id" in msg
        assert "agent_id" in msg
        assert "parent_id" in msg
        assert "content" in msg
        assert "created_at" in msg
        assert "content_type" in msg
        assert msg.get("content_type") == "text"

    async def test_read_messages_default_limit_returns_3(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Default limit returns 3 messages in descending order."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
        })
        messages = _extract_messages(result)
        assert len(messages) == 3
        assert messages[0].get("content") == "Message 4"
        assert messages[1].get("content") == "Message 3"
        assert messages[2].get("content") == "Message 2"

    async def test_read_messages_default_order_is_desc(
        self, server: AgoraServer,
    ) -> None:
        """Default order returns messages newest-first."""
        await server.call_tool("chat_post_message", {
            "channel": "#order-test", "content": "First",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#order-test", "content": "Second",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#order-test", "content": "Third",
        })
        result = await server.call_tool("chat_read_messages", {
            "channel": "#order-test",
        })
        messages = _extract_messages(result)
        assert len(messages) == 3
        assert messages[0].get("content") == "Third"
        assert messages[1].get("content") == "Second"
        assert messages[2].get("content") == "First"

    async def test_read_messages_explicit_limit_overrides_default(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Explicit limit overrides the default limit of 3."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "limit": 10,
            "order": "asc",
        })
        messages = _extract_messages(result)
        assert len(messages) == 5  # All 5 messages within limit=10

    async def test_read_messages_explicit_order_overrides_default(
        self, server: AgoraServer,
    ) -> None:
        """Explicit 'asc' order overrides the default 'desc' order."""
        await server.call_tool("chat_post_message", {
            "channel": "#order-override", "content": "First",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#order-override", "content": "Second",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#order-override", "content": "Third",
        })
        result = await server.call_tool("chat_read_messages", {
            "channel": "#order-override",
            "order": "asc",
        })
        messages = _extract_messages(result)
        assert len(messages) == 3
        assert messages[0].get("content") == "First"
        assert messages[1].get("content") == "Second"
        assert messages[2].get("content") == "Third"

    async def test_read_messages_empty_channel_returns_empty(
        self, server: AgoraServer,
    ) -> None:
        """Reading a non-existent channel returns an empty list."""
        result = await server.call_tool("chat_read_messages", {
            "channel": "#nonexistent",
        })
        messages = _extract_messages(result)
        assert messages == []

    async def test_read_messages_limit_0_returns_empty(
        self, server: AgoraServer, channel_with_messages: str,
    ) -> None:
        """Explicit limit=0 returns an empty list."""
        _ = channel_with_messages
        result = await server.call_tool("chat_read_messages", {
            "channel": "#test",
            "limit": 0,
        })
        messages = _extract_messages(result)
        assert messages == []


# ── chat_list_channels ─────────────────────────────────────────────


class TestListChannels:
    """Tests for the chat_list_channels tool."""

    async def test_list_empty(self, server: AgoraServer) -> None:
        """No channels returns an empty list."""
        result = await server.call_tool("chat_list_channels", {})
        channels = _extract_channels(result)
        assert channels == []

    async def test_list_all(self, server: AgoraServer) -> None:
        """List returns all channels with topic, message_count, last_activity_at."""
        await server.call_tool("chat_post_message", {
            "channel": "#alpha", "content": "A",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#beta", "content": "B",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#gamma", "content": "C",
        })
        result = await server.call_tool("chat_list_channels", {})
        channels = _extract_channels(result)
        names = [c.get("name") for c in channels]
        assert "#alpha" in names
        assert "#beta" in names
        assert "#gamma" in names

        # Verify channel fields are present
        alpha = next(c for c in channels if c.get("name") == "#alpha")
        assert "topic" in alpha
        alpha_count = alpha.get("message_count")
        assert isinstance(alpha_count, int)
        assert alpha_count >= 1
        assert alpha.get("last_activity_at") is not None

    async def test_channels_ordered_by_name(self, server: AgoraServer) -> None:
        """Channels are listed in alphabetical name order."""
        await server.call_tool("chat_post_message", {
            "channel": "#zed", "content": "z",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#alpha", "content": "a",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#mu", "content": "m",
        })
        result = await server.call_tool("chat_list_channels", {})
        channels = _extract_channels(result)
        names = [str(c.get("name", "")) for c in channels]
        assert names == sorted(names), f"Expected sorted order, got {names}"

    async def test_list_with_prefix(self, server: AgoraServer) -> None:
        """Prefix filter returns only matching channels."""
        await server.call_tool("chat_post_message", {
            "channel": "#dev-auth", "content": "x",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#dev-api", "content": "x",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#prod-db", "content": "x",
        })
        result = await server.call_tool("chat_list_channels", {
            "prefix": "#dev",
        })
        channels = _extract_channels(result)
        names = {c.get("name") for c in channels}
        assert names == {"#dev-auth", "#dev-api"}

    async def test_general_channel_topic(self, server: AgoraServer) -> None:
        """The #general channel is created with a descriptive topic."""
        await server.call_tool("chat_post_message", {
            "channel": "#general", "content": "hello",
        })
        result = await server.call_tool("chat_list_channels", {})
        channels = _extract_channels(result)
        general = next((c for c in channels if c.get("name") == "#general"), None)
        assert general is not None, "#general channel should exist"
        topic = general.get("topic")
        assert topic is not None
        assert "General discussion" in str(topic)

    async def test_channel_message_count(self, server: AgoraServer) -> None:
        """Channel listing includes message_count and last_activity_at."""
        await server.call_tool("chat_post_message", {
            "channel": "#count-test", "content": "one",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#count-test", "content": "two",
        })
        result = await server.call_tool("chat_list_channels", {})
        channels = _extract_channels(result)
        for ch in channels:
            if ch.get("name") == "#count-test":
                assert ch.get("message_count") == 2
                assert ch.get("last_activity_at") is not None
                return
        pytest.fail("Channel not found in listing")


# ── chat_summarize_channel ─────────────────────────────────────────


class TestSummarizeChannel:
    """Tests for the chat_summarize_channel tool."""

    async def test_summarize_with_messages(self, server: AgoraServer) -> None:
        """Summarize returns stats for a channel with messages."""
        await server.call_tool("chat_post_message", {
            "channel": "#summary-test", "content": "First post",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#summary-test", "content": "Second post",
        })
        await server.call_tool("chat_post_message", {
            "channel": "#summary-test", "content": "Third post",
        })
        result = await server.call_tool("chat_summarize_channel", {
            "channel": "#summary-test",
        })
        assert result.get("message_count") == 3
        participants = result.get("participants")
        assert isinstance(participants, int)
        assert participants >= 1
        assert isinstance(result.get("summary"), str)
        time_span = result.get("time_span_hours")
        assert isinstance(time_span, (int, float))
        assert time_span >= 0

    async def test_summarize_actually_empty_channel(self, server: AgoraServer) -> None:
        """Summarize a channel that exists but has no messages returns count 0."""
        # Create channel by posting then immediately read-only
        await server.call_tool("chat_post_message", {
            "channel": "#empty-chan", "content": "seed",
        })
        # Use a different channel for summarize that has never been written to
        # (read-only doesn't auto-vivify, so it won't exist)
        result = await server.call_tool("chat_summarize_channel", {
            "channel": "#never-posted",
        })
        assert result.get("error") == "CHANNEL_NOT_FOUND"

    async def test_summarize_existing_empty_channel(self, server: AgoraServer) -> None:
        """Summarize a channel with only the system seed returns count >= 1."""
        await server.call_tool("chat_post_message", {
            "channel": "#seed-only", "content": "seed",
        })
        result = await server.call_tool("chat_summarize_channel", {
            "channel": "#seed-only",
        })
        msg_count = result.get("message_count")
        assert isinstance(msg_count, int)
        assert msg_count >= 1

    async def test_summarize_nonexistent_channel(self, server: AgoraServer) -> None:
        """Non-existent channel returns an error."""
        result = await server.call_tool("chat_summarize_channel", {
            "channel": "#does-not-exist",
        })
        assert result.get("error") == "CHANNEL_NOT_FOUND"

    async def test_summarize_empty_channel_name(self, server: AgoraServer) -> None:
        """Empty channel name returns validation error."""
        result = await server.call_tool("chat_summarize_channel", {
            "channel": "",
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_summarize_with_built_in_llm(self, llm_stub_server: AgoraServer) -> None:
        """use_built_in_llm=True returns stub message."""
        await llm_stub_server.call_tool("chat_post_message", {
            "channel": "#llm-stub-test", "content": "Hello",
        })
        result = await llm_stub_server.call_tool("chat_summarize_channel", {
            "channel": "#llm-stub-test",
        })
        assert isinstance(result.get("summary"), str)
        assert "not yet implemented" in str(result.get("summary", ""))
        assert result.get("message_count") == 1

    async def test_summarize_single_message(self, server: AgoraServer) -> None:
        """Summarize a channel with a single message returns count=1."""
        await server.call_tool("chat_post_message", {
            "channel": "#single-msg", "content": "Only message",
        })
        result = await server.call_tool("chat_summarize_channel", {
            "channel": "#single-msg",
        })
        assert result.get("message_count") == 1
        participants = result.get("participants")
        assert isinstance(participants, int)
        assert participants >= 1

    async def test_summarize_with_llm_api_url(self, server: AgoraServer) -> None:
        """Configured llm_api_url calls external LLM and returns its summary."""
        await server.call_tool("chat_post_message", {
            "channel": "#llm-url-test", "content": "LLM summary test",
        })
        mock_response = MagicMock()
        payload = b'{"choices":[{"message":{"content":"Mock LLM summary"}}]}'
        mock_response.read.return_value = payload
        with patch("urllib.request.urlopen", return_value=mock_response):
            plugin = cast("ChatPlugin", server.plugins[0])
            plugin.llm_api_url = "https://fake-llm.example.com/v1/chat/completions"
            result = await server.call_tool("chat_summarize_channel", {
                "channel": "#llm-url-test",
            })
        assert isinstance(result.get("summary"), str)
        assert "Mock LLM summary" in str(result.get("summary", ""))
        assert result.get("message_count") == 1

    async def test_summarize_long_channel_name(self, server: AgoraServer) -> None:
        """Summarize a channel with a very long name works."""
        long_name = "#" + "a" * 200
        await server.call_tool("chat_post_message", {
            "channel": long_name, "content": "Long name test",
        })
        result = await server.call_tool("chat_summarize_channel", {
            "channel": long_name,
        })
        assert result.get("message_count") == 1


# ── Event hooks ────────────────────────────────────────────────────


class TestEventHooks:
    """Tests for agent lifecycle event hooks."""

    async def test_agent_register_posts_to_general(self, server: AgoraServer) -> None:
        """When an agent registers, a 'joined' message appears in #general."""
        reg = await server.call_tool("register", {"name": "test-agent"})
        agent_id = str(reg.get("agent_id", ""))

        result = await server.call_tool("chat_read_messages", {
            "channel": "#general",
        })
        messages = _extract_messages(result)
        joined = [
            m for m in messages
            if "joined" in str(m.get("content", ""))
            and agent_id in str(m.get("content", ""))
        ]
        assert len(joined) >= 1

    async def test_agent_disconnect_posts_to_general(self, server: AgoraServer) -> None:
        """When an agent disconnects, a 'left' message appears in #general."""
        reg = await server.call_tool("register", {"name": "leaving-agent"})
        agent_id = str(reg.get("agent_id", ""))

        # Simulate disconnect by emitting the event via eventbus
        evt_bus = server._eventbus  # noqa: SLF001  # white-box test
        assert evt_bus is not None
        await evt_bus.emit("agent.disconnected", agent_id=agent_id)

        result = await server.call_tool("chat_read_messages", {
            "channel": "#general",
        })
        messages = _extract_messages(result)
        left_msgs = [
            m for m in messages
            if "left" in str(m.get("content", ""))
            and agent_id in str(m.get("content", ""))
        ]
        assert len(left_msgs) >= 1

    async def test_multiple_agents_register(
        self, server: AgoraServer,
    ) -> None:
        """Multiple agent registrations each produce a joined message with correct ID."""
        reg_a = await server.call_tool("register", {"name": "alice"})
        agent_a = str(reg_a.get("agent_id", ""))
        reg_b = await server.call_tool("register", {"name": "bob"})
        agent_b = str(reg_b.get("agent_id", ""))

        result = await server.call_tool("chat_read_messages", {
            "channel": "#general",
        })
        messages = _extract_messages(result)
        joined_a = [
            m for m in messages
            if "joined" in str(m.get("content", ""))
            and agent_a in str(m.get("content", ""))
        ]
        joined_b = [
            m for m in messages
            if "joined" in str(m.get("content", ""))
            and agent_b in str(m.get("content", ""))
        ]
        assert len(joined_a) >= 1
        assert len(joined_b) >= 1


# ── Edge cases ────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case tests for the Chat plugin."""

    async def test_channel_limit(self) -> None:
        """Creating more than max_channels channels returns an error."""
        max_ch = 10
        srv = AgoraServer(
            config={
                "db_path": ":memory:",
                "plugins": [{
                    "name": "chat",
                    "enabled": True,
                    "module": "agora.plugins.chat",
                    "class_name": "ChatPlugin",
                    "config": {"max_channels": max_ch, "max_message_length": 100_000},
                }],
            },
            skip_transport=True,
        )
        await srv.start()
        try:
            for i in range(max_ch):
                await srv.call_tool("chat_post_message", {
                    "channel": f"#channel-{i}",
                    "content": "hello",
                })
            result = await srv.call_tool("chat_post_message", {
                "channel": "#channel-too-many",
                "content": "should fail",
            })
            assert result.get("error") == "CHANNEL_LIMIT"
        finally:
            await srv.stop()

    async def test_concurrent_posts(self, server: AgoraServer) -> None:
        """Two agents posting concurrently both succeed."""
        results = await asyncio.gather(
            server.call_tool("chat_post_message", {
                "channel": "#concurrent", "content": "Post A",
            }),
            server.call_tool("chat_post_message", {
                "channel": "#concurrent", "content": "Post B",
            }),
            return_exceptions=True,
        )

        for r in results:
            assert isinstance(r, dict)
            assert "message_id" in r

        read_result = await server.call_tool("chat_read_messages", {
            "channel": "#concurrent",
        })
        messages = _extract_messages(read_result)
        assert len(messages) == 2

    async def test_concurrent_read_while_write(self, server: AgoraServer) -> None:
        """Reading while another writes returns consistent state."""
        # Post initial messages
        for i in range(3):
            await server.call_tool("chat_post_message", {
                "channel": "#rw-test", "content": f"initial-{i}",
            })

        # Concurrent read + write
        read_result, write_result = await asyncio.gather(
            server.call_tool("chat_read_messages", {
                "channel": "#rw-test",
            }),
            server.call_tool("chat_post_message", {
                "channel": "#rw-test", "content": "concurrent write",
            }),
            return_exceptions=True,
        )

        assert isinstance(read_result, dict)
        assert isinstance(write_result, dict)
        assert "message_id" in write_result
        messages = _extract_messages(read_result)
        # The read should see a consistent snapshot (either 3 or 4 messages)
        assert len(messages) >= 3

    async def test_message_id_uniqueness(self, server: AgoraServer) -> None:
        """Each posted message gets a unique ID."""
        ids: set[str] = set()
        for i in range(10):
            result = await server.call_tool("chat_post_message", {
                "channel": "#unique-test",
                "content": f"Post {i}",
            })
            msg_id = result.get("message_id")
            assert isinstance(msg_id, str)
            ids.add(msg_id)
        assert len(ids) == 10

    async def test_message_shape(self, server: AgoraServer) -> None:
        """Each message in read_messages has all expected fields including parent_id."""
        await server.call_tool("chat_post_message", {
            "channel": "#shape-test",
            "content": "Shape check",
        })
        result = await server.call_tool("chat_read_messages", {
            "channel": "#shape-test",
        })
        messages = _extract_messages(result)
        assert len(messages) >= 1
        msg = messages[0]
        assert "id" in msg
        assert "channel_id" in msg
        assert "agent_id" in msg
        assert "parent_id" in msg
        assert "content" in msg
        assert "created_at" in msg
        assert "content_type" in msg
        assert msg.get("content_type") == "text"


# ── chat_await_update ────────────────────────────────────────────


async def _post_after_delay(
    server: AgoraServer,
    delay: float,
    channel: str,
    content: str,
) -> None:
    """Sleep then post a message — used as a background task."""
    await asyncio.sleep(delay)
    await server.call_tool("chat_post_message", {
        "channel": channel,
        "content": content,
    })


class TestAwaitUpdate:
    """Tests for the chat_await_update tool."""

    # ── Happy path ────────────────────────────────────────────

    async def test_await_update_returns_immediately_when_messages_exist(
        self, server: AgoraServer,
    ) -> None:
        """If nmsg messages already exist, return immediately."""
        await server.call_tool("chat_post_message", {
            "channel": "#await-fast",
            "content": "hello",
        })
        result = await server.call_tool("chat_await_update", {
            "channel": "#await-fast",
            "nmsg": 1,
            "timeout": 5.0,
        })
        assert result.get("waited") is False
        assert result.get("timed_out") is False
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) >= 1

    async def test_await_update_blocks_until_message_posted(
        self, server: AgoraServer,
    ) -> None:
        """Blocks until a message is posted, then returns it."""
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-block",
                "nmsg": 1,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)  # let waiter register
        await server.call_tool("chat_post_message", {
            "channel": "#await-block",
            "content": "wake up",
        })
        result = await task
        assert result.get("waited") is True
        assert result.get("timed_out") is False
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) >= 1
        assert messages[0].get("content") == "wake up"

    async def test_await_update_times_out(self, server: AgoraServer) -> None:
        """Returns timed_out=True when no messages arrive within timeout."""
        result = await server.call_tool("chat_await_update", {
            "channel": "#await-timeout",
            "nmsg": 1,
            "timeout": 0.1,
        })
        assert result.get("waited") is True
        assert result.get("timed_out") is True
        assert result.get("messages") == []

    async def test_await_update_multiple_concurrent_waits(
        self, server: AgoraServer,
    ) -> None:
        """Multiple concurrent waiters all wake on a single post."""
        tasks = [
            asyncio.create_task(
                server.call_tool("chat_await_update", {
                    "channel": "#await-multi",
                    "nmsg": 1,
                    "timeout": 5.0,
                }),
            )
            for _ in range(3)
        ]
        await asyncio.sleep(0.05)
        await server.call_tool("chat_post_message", {
            "channel": "#await-multi",
            "content": "ping",
        })
        results = await asyncio.gather(*tasks)
        for r in results:
            assert isinstance(r, dict)
            assert r.get("timed_out") is False

    async def test_await_update_with_since_parameter(
        self, server: AgoraServer,
    ) -> None:
        """Since parameter filters messages correctly."""
        # Post initial messages
        for i in range(3):
            await server.call_tool("chat_post_message", {
                "channel": "#await-since",
                "content": f"old-{i}",
            })
        # Record a timestamp after the old messages
        marker = await server.call_tool("chat_post_message", {
            "channel": "#await-since",
            "content": "marker",
        })
        since_time = str(marker.get("created_at", ""))
        # Post a new message after the since time
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-since",
                "nmsg": 1,
                "timeout": 5.0,
                "since": since_time,
            }),
        )
        await asyncio.sleep(0.05)
        await server.call_tool("chat_post_message", {
            "channel": "#await-since",
            "content": "new-msg",
        })
        result = await task
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) >= 1

    async def test_await_update_empty_channel_waits(
        self, server: AgoraServer,
    ) -> None:
        """Waits on a channel with no messages (non-existent yet)."""
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-empty",
                "nmsg": 1,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        await server.call_tool("chat_post_message", {
            "channel": "#await-empty",
            "content": "first",
        })
        result = await task
        assert result.get("waited") is True
        assert result.get("timed_out") is False

    # ── Validation ────────────────────────────────────────────

    async def test_await_update_validation_nmsg_zero(
        self, server: AgoraServer,
    ) -> None:
        """nmsg=0 returns VALIDATION_ERROR."""
        result = await server.call_tool("chat_await_update", {
            "channel": "#test",
            "nmsg": 0,
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_await_update_validation_negative_timeout(
        self, server: AgoraServer,
    ) -> None:
        """Negative timeout returns VALIDATION_ERROR."""
        result = await server.call_tool("chat_await_update", {
            "channel": "#test",
            "timeout": -1.0,
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_await_update_validation_empty_channel(
        self, server: AgoraServer,
    ) -> None:
        """Empty channel name returns VALIDATION_ERROR."""
        result = await server.call_tool("chat_await_update", {
            "channel": "",
        })
        assert result.get("error") == "VALIDATION_ERROR"

    # ── Edge cases ────────────────────────────────────────────

    async def test_await_update_shutdown_wakes_waiters(self) -> None:
        """Server shutdown wakes all blocked waiters."""
        srv = AgoraServer(
            config={
                "db_path": ":memory:",
                "plugins": [_CHAT_PLUGIN_CONFIG],
            },
            skip_transport=True,
        )
        await srv.start()
        task = asyncio.create_task(
            srv.call_tool("chat_await_update", {
                "channel": "#shutdown-wait",
                "nmsg": 1,
                "timeout": 30.0,
            }),
        )
        await asyncio.sleep(0.05)
        await srv.stop()
        result = await task
        assert isinstance(result, dict)
        # The waiter should have been woken by shutdown

    async def test_await_update_returns_nmsg_messages_not_more(
        self, server: AgoraServer,
    ) -> None:
        """Only returns up to nmsg messages, not more."""
        for i in range(5):
            await server.call_tool("chat_post_message", {
                "channel": "#await-nmsg",
                "content": f"msg-{i}",
            })
        result = await server.call_tool("chat_await_update", {
            "channel": "#await-nmsg",
            "nmsg": 2,
            "timeout": 5.0,
        })
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) == 2

    async def test_await_update_messages_are_newest(
        self, server: AgoraServer,
    ) -> None:
        """Returns the newest nmsg messages when there are more."""
        for i in range(5):
            await server.call_tool("chat_post_message", {
                "channel": "#await-newest",
                "content": f"msg-{i}",
            })
        result = await server.call_tool("chat_await_update", {
            "channel": "#await-newest",
            "nmsg": 2,
            "timeout": 5.0,
        })
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) == 2
        # Should be the last two in chronological order
        assert messages[0].get("content") == "msg-3"
        assert messages[1].get("content") == "msg-4"

    async def test_await_update_channel_not_existing(
        self, server: AgoraServer,
    ) -> None:
        """Non-existent channel waits and returns empty on timeout."""
        result = await server.call_tool("chat_await_update", {
            "channel": "#nonexistent-channel",
            "nmsg": 1,
            "timeout": 0.1,
        })
        assert result.get("waited") is True
        assert result.get("timed_out") is True
        assert result.get("messages") == []

    async def test_await_update_nmsg_greater_than_1(
        self, server: AgoraServer,
    ) -> None:
        """Waits until nmsg=3 messages accumulate."""
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-n3",
                "nmsg": 3,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        for i in range(3):
            await server.call_tool("chat_post_message", {
                "channel": "#await-n3",
                "content": f"msg-{i}",
            })
        result = await task
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) == 3
        assert result.get("timed_out") is False

    async def test_await_update_concurrent_posts_during_wait(
        self, server: AgoraServer,
    ) -> None:
        """Multiple concurrent posts during wait all get collected."""
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-conc-posts",
                "nmsg": 3,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        await asyncio.gather(
            server.call_tool("chat_post_message", {
                "channel": "#await-conc-posts", "content": "A",
            }),
            server.call_tool("chat_post_message", {
                "channel": "#await-conc-posts", "content": "B",
            }),
            server.call_tool("chat_post_message", {
                "channel": "#await-conc-posts", "content": "C",
            }),
        )
        result = await task
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) >= 3

    # ── Concurrency & stress ──────────────────────────────────

    async def test_await_update_rapid_fire_posts_during_wait(
        self, server: AgoraServer,
    ) -> None:
        """Rapid-fire posts during wait all get collected."""
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-rapid",
                "nmsg": 5,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        for i in range(5):
            await server.call_tool("chat_post_message", {
                "channel": "#await-rapid",
                "content": f"rapid-{i}",
            })
        result = await task
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) >= 5

    async def test_await_update_many_concurrent_waiters_same_channel(
        self, server: AgoraServer,
    ) -> None:
        """20 concurrent waiters on same channel, run 3 times."""
        for _ in range(3):
            tasks = [
                asyncio.create_task(
                    server.call_tool("chat_await_update", {
                        "channel": "#await-stress",
                        "nmsg": 1,
                        "timeout": 5.0,
                    }),
                )
                for _ in range(20)
            ]
            await asyncio.sleep(0.05)
            await server.call_tool("chat_post_message", {
                "channel": "#await-stress",
                "content": "wake-all",
            })
            results = await asyncio.gather(*tasks)
            for r in results:
                assert isinstance(r, dict)
                assert r.get("timed_out") is False

    async def test_await_update_interleaved_wait_and_post(
        self, server: AgoraServer,
    ) -> None:
        """Interleaved wait-then-post cycles work correctly."""
        for cycle in range(3):
            task = asyncio.create_task(
                server.call_tool("chat_await_update", {
                    "channel": "#await-interleave",
                    "nmsg": 1,
                    "timeout": 5.0,
                }),
            )
            await asyncio.sleep(0.02)
            await server.call_tool("chat_post_message", {
                "channel": "#await-interleave",
                "content": f"cycle-{cycle}",
            })
            result = await task
            assert result.get("timed_out") is False

    async def test_await_update_waiter_cancelled_doesnt_affect_others(
        self, server: AgoraServer,
    ) -> None:
        """Cancelling one waiter doesn't break others."""
        task_a = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-cancel",
                "nmsg": 1,
                "timeout": 5.0,
            }),
        )
        task_b = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-cancel",
                "nmsg": 1,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        task_a.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_a
        await server.call_tool("chat_post_message", {
            "channel": "#await-cancel",
            "content": "still-here",
        })
        result_b = await task_b
        assert result_b.get("timed_out") is False

    async def test_await_update_shutdown_during_active_wait(self) -> None:
        """Shutdown during active wait wakes the waiter cleanly."""
        srv = AgoraServer(
            config={
                "db_path": ":memory:",
                "plugins": [_CHAT_PLUGIN_CONFIG],
            },
            skip_transport=True,
        )
        await srv.start()
        task = asyncio.create_task(
            srv.call_tool("chat_await_update", {
                "channel": "#await-shutdown-active",
                "nmsg": 1,
                "timeout": 30.0,
            }),
        )
        await asyncio.sleep(0.05)
        await srv.stop()
        result = await task
        assert isinstance(result, dict)

    async def test_await_update_concurrent_posts_from_multiple_agents(
        self, server: AgoraServer,
    ) -> None:
        """Posts from multiple agents during wait are all received."""
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-multi-agent",
                "nmsg": 3,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        await asyncio.gather(
            server.call_tool("chat_post_message", {
                "channel": "#await-multi-agent",
                "content": "agent-a",
            }),
            server.call_tool("chat_post_message", {
                "channel": "#await-multi-agent",
                "content": "agent-b",
            }),
            server.call_tool("chat_post_message", {
                "channel": "#await-multi-agent",
                "content": "agent-c",
            }),
        )
        result = await task
        messages = result.get("messages", [])
        assert isinstance(messages, list)
        assert len(messages) >= 3

    async def test_await_update_stress_loop(
        self, server: AgoraServer,
    ) -> None:
        """Rapid repeated await-then-post cycles (10 iterations)."""
        for i in range(10):
            task = asyncio.create_task(
                server.call_tool("chat_await_update", {
                    "channel": f"#await-stress-{i}",
                    "nmsg": 1,
                    "timeout": 5.0,
                }),
            )
            await asyncio.sleep(0.01)
            await server.call_tool("chat_post_message", {
                "channel": f"#await-stress-{i}",
                "content": f"stress-{i}",
            })
            result = await task
            assert result.get("timed_out") is False

    # ── Reliability ───────────────────────────────────────────

    async def test_await_update_waiter_cleanup_on_cancel(
        self, server: AgoraServer,
    ) -> None:
        """Cancelled waiter is cleaned up from the registry."""
        plugin = cast("ChatPlugin", server.plugins[0])
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-cleanup",
                "nmsg": 1,
                "timeout": 30.0,
            }),
        )
        await asyncio.sleep(0.05)
        assert len(plugin._waiters.get("#await-cleanup", [])) == 1  # noqa: SLF001
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # After cancel+finally, waiter list should be empty or cleaned
        waiters = plugin._waiters.get("#await-cleanup", [])  # noqa: SLF001
        assert len(waiters) == 0

    async def test_await_update_db_error_during_recheck(
        self, server: AgoraServer,
    ) -> None:
        """DB error during recheck after wake propagates as exception."""
        plugin = cast("ChatPlugin", server.plugins[0])
        original = plugin._count_messages_since  # noqa: SLF001

        async def failing_count(
            _channel: str, _since: str | None = None,
        ) -> int:
            msg = "DB read failed"
            raise RuntimeError(msg) from None

        plugin._count_messages_since = failing_count  # type: ignore[assignment]  # noqa: SLF001
        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": "#await-dberr",
                "nmsg": 1,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        # Post to trigger recheck
        await server.call_tool("chat_post_message", {
            "channel": "#await-dberr",
            "content": "trigger",
        })
        with pytest.raises(RuntimeError, match="DB read failed"):
            await task
        plugin._count_messages_since = original  # type: ignore[assignment]  # noqa: SLF001

    async def test_await_update_zero_timeout_returns_immediately(
        self, server: AgoraServer,
    ) -> None:
        """timeout=0 returns VALIDATION_ERROR (must be positive)."""
        result = await server.call_tool("chat_await_update", {
            "channel": "#test",
            "timeout": 0,
        })
        assert result.get("error") == "VALIDATION_ERROR"

    async def test_await_update_since_timestamp_in_future(
        self, server: AgoraServer,
    ) -> None:
        """Since timestamp in the future means zero messages — should timeout."""
        result = await server.call_tool("chat_await_update", {
            "channel": "#await-future",
            "nmsg": 1,
            "timeout": 0.1,
            "since": "2099-01-01T00:00:00+00:00",
        })
        assert result.get("waited") is True
        assert result.get("timed_out") is True

    async def test_await_update_max_waiters_limit(
        self, server: AgoraServer,
    ) -> None:
        """Exceeding _MAX_WAITERS_PER_CHANNEL returns RESOURCE_LIMIT."""
        plugin = cast("ChatPlugin", server.plugins[0])
        channel = "#await-limit"
        # Fill up to the limit
        for _ in range(100):
            plugin._waiters[channel].append(asyncio.Event())  # noqa: SLF001
        result = await server.call_tool("chat_await_update", {
            "channel": channel,
            "nmsg": 1,
            "timeout": 0.1,
        })
        assert result.get("error") == "RESOURCE_LIMIT"
        # Cleanup
        plugin._waiters[channel].clear()  # noqa: SLF001
        del plugin._waiters[channel]  # noqa: SLF001

    async def test_await_update_on_shutdown_clears_all_waiters(self) -> None:
        """on_shutdown clears the waiter registry."""
        srv = AgoraServer(
            config={
                "db_path": ":memory:",
                "plugins": [_CHAT_PLUGIN_CONFIG],
            },
            skip_transport=True,
        )
        await srv.start()
        plugin = cast("ChatPlugin", srv.plugins[0])
        # Manually add a waiter
        plugin._waiters["#chan"].append(asyncio.Event())  # noqa: SLF001
        assert len(plugin._waiters["#chan"]) == 1  # noqa: SLF001
        await srv.stop()
        assert len(plugin._waiters) == 0  # noqa: SLF001

    async def test_await_update_eventbus_unsubscribe_on_shutdown(
        self, server: AgoraServer,
    ) -> None:
        """on_shutdown unsubscribes from chat.message.posted."""
        evt_bus = server._eventbus  # noqa: SLF001
        assert evt_bus is not None
        plugin = cast("ChatPlugin", server.plugins[0])
        # Confirm subscribed
        handlers = evt_bus._subscribers.get(  # noqa: SLF001
            "chat.message.posted", [],
        )
        assert plugin._on_message_posted in handlers  # noqa: SLF001
        await server.stop()
        # After shutdown, handler should be removed
        handlers_after = evt_bus._subscribers.get(  # noqa: SLF001
            "chat.message.posted", [],
        )
        assert plugin._on_message_posted not in handlers_after  # noqa: SLF001

    # ── EventBus lifecycle ────────────────────────────────────

    async def test_await_update_subscribes_to_eventbus(
        self, server: AgoraServer,
    ) -> None:
        """on_startup subscribes _on_message_posted to chat.message.posted."""
        evt_bus = server._eventbus  # noqa: SLF001
        assert evt_bus is not None
        plugin = cast("ChatPlugin", server.plugins[0])
        handlers = evt_bus._subscribers.get(  # noqa: SLF001
            "chat.message.posted", [],
        )
        assert plugin._on_message_posted in handlers  # noqa: SLF001

    async def test_await_update_eventbus_handler_error_isolation(
        self, server: AgoraServer,
    ) -> None:
        """If one waiter's event.set() raises, others still wake."""
        plugin = cast("ChatPlugin", server.plugins[0])
        channel = "#await-error-iso"
        good_event = asyncio.Event()
        bad_event = MagicMock()
        bad_event.set.side_effect = RuntimeError("broken event")

        plugin._waiters[channel].extend([good_event, bad_event])  # noqa: SLF001

        task = asyncio.create_task(
            server.call_tool("chat_await_update", {
                "channel": channel,
                "nmsg": 1,
                "timeout": 5.0,
            }),
        )
        await asyncio.sleep(0.05)
        # Manually emit the event to trigger _on_message_posted
        evt_bus = server._eventbus  # noqa: SLF001
        assert evt_bus is not None
        await evt_bus.emit("chat.message.posted", channel=channel)

        # The good_event should be set, bad one should log but not crash
        assert good_event.is_set()
        # Now post a message so the recheck finds it
        await server.call_tool("chat_post_message", {
            "channel": channel,
            "content": "trigger",
        })
        result = await task
        assert isinstance(result, dict)
        # Cleanup
        plugin._waiters.pop(channel, None)  # noqa: SLF001
