"""End-to-end integration tests for the Chat plugin.

Scenario-based tests covering realistic multi-agent workflows that chain
multiple Chat plugin tools together: register → post → read → summarize →
list channels. Each test exercises a realistic workflow rather than a
single tool in isolation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from agora.backbone.server import AgoraServer
from agora.plugins.chat import ChatPlugin

_CHAT_CONFIG: dict[str, object] = {
    "name": "chat",
    "enabled": True,
    "module": "agora.plugins.chat",
    "class_name": "ChatPlugin",
    "config": {
        "max_message_length": 100_000,
        "max_channels": 10,  # Low limit for testing
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


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a running AgoraServer with Chat plugin (max_channels=10)."""
    srv = AgoraServer(
        config={"db_path": ":memory:", "plugins": [_CHAT_CONFIG]},
        skip_transport=True,
    )
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
async def llm_stub_server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a running server with use_built_in_llm=True."""
    srv = AgoraServer(
        config={"db_path": ":memory:", "plugins": [_LLM_STUB_CONFIG]},
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


# ── Integration scenarios ──────────────────────────────────────────


class TestChatIntegration:
    """End-to-end integration scenarios for the Chat plugin."""

    async def test_full_agent_workflow(self, server: AgoraServer) -> None:
        """Register an agent, post a message, and read it back with correct metadata."""
        # Given: an agent is registered with the backbone
        reg = await server.call_tool("register", {"name": "scout"})
        agent_id = str(reg.get("agent_id", ""))
        assert len(agent_id) == 36

        listed = await server.call_tool("list_agents", {})
        raw_agents = listed.get("agents", [])
        agents = raw_agents if isinstance(raw_agents, list) else []
        names = [str(a.get("name", "")) for a in agents if isinstance(a, dict)]
        assert "scout" in names

        # When: the agent posts to #general (simulated with _agent_id)
        post = await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "Hello from scout!",
            "_agent_id": agent_id,
        })
        assert "message_id" in post

        # Then: reading #general returns the message with correct agent_id
        read = await server.call_tool("chat_read_messages", {"channel": "#general"})
        messages = _extract_messages(read)
        scout_msgs = [
            m for m in messages
            if m.get("content") == "Hello from scout!"
            and m.get("agent_id") == agent_id
        ]
        assert len(scout_msgs) == 1

    async def test_multi_agent_conversation(self, server: AgoraServer) -> None:
        """Two agents post to #general and a new channel is auto-created."""
        # Given: two agents post to #general
        reg_a = await server.call_tool("register", {"name": "alice"})
        agent_a = str(reg_a.get("agent_id", ""))
        reg_b = await server.call_tool("register", {"name": "bob"})
        agent_b = str(reg_b.get("agent_id", ""))

        # When: both agents post different messages
        await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "Hi from alice",
            "_agent_id": agent_a,
        })
        await server.call_tool("chat_post_message", {
            "channel": "#general",
            "content": "Hi from bob",
            "_agent_id": agent_b,
        })

        # Then: both messages present in order with correct agent_ids
        read = await server.call_tool("chat_read_messages", {"channel": "#general"})
        messages = _extract_messages(read)
        agent_msgs = [m for m in messages if m.get("agent_id") in (agent_a, agent_b)]
        assert len(agent_msgs) == 2
        assert agent_msgs[0].get("agent_id") == agent_a
        assert agent_msgs[1].get("agent_id") == agent_b

        # When: posting to a NEW channel
        await server.call_tool("chat_post_message", {
            "channel": "#random",
            "content": "Off-topic!",
        })

        # Then: channel is auto-created and listed
        channels = _extract_channels(
            await server.call_tool("chat_list_channels", {}),
        )
        names = [str(c.get("name", "")) for c in channels]
        assert "#random" in names

    async def test_threaded_conversation(self, server: AgoraServer) -> None:
        """Post a parent message with 2 replies, verify threading and counts."""
        # Given: a parent message
        parent = await server.call_tool("chat_post_message", {
            "channel": "#threaded",
            "content": "Original thought",
        })
        parent_id = str(parent.get("message_id", ""))

        # When: 2 replies are posted with parent_id
        await server.call_tool("chat_post_message", {
            "channel": "#threaded",
            "content": "Reply 1",
            "parent_id": parent_id,
        })
        await server.call_tool("chat_post_message", {
            "channel": "#threaded",
            "content": "Reply 2",
            "parent_id": parent_id,
        })

        # Then: all 3 messages present, replies have correct parent_id
        read = await server.call_tool("chat_read_messages", {"channel": "#threaded"})
        messages = _extract_messages(read)
        assert len(messages) == 3
        replies = [m for m in messages if m.get("parent_id") == parent_id]
        assert len(replies) == 2

        # Then: channel listing shows message_count=3
        channels = _extract_channels(
            await server.call_tool("chat_list_channels", {}),
        )
        threaded = next(
            (c for c in channels if c.get("name") == "#threaded"), None,
        )
        assert threaded is not None
        assert threaded.get("message_count") == 3

    async def test_channel_listing_and_filtering(self, server: AgoraServer) -> None:
        """Create channels with different prefixes and verify prefix filtering."""
        # Given: channels with #dev-*, #prod-*, #personal-* prefixes
        for name in ["#dev-auth", "#dev-api", "#prod-db", "#prod-cache", "#personal-notes"]:
            await server.call_tool("chat_post_message", {
                "channel": name, "content": f"post to {name}",
            })

        # When: listing all channels
        all_ch = _extract_channels(
            await server.call_tool("chat_list_channels", {}),
        )

        # Then: all 5 channels present
        all_names = {str(c.get("name", "")) for c in all_ch}
        expected = {"#dev-auth", "#dev-api", "#prod-db", "#prod-cache", "#personal-notes"}
        assert expected.issubset(all_names)

        # When: listing with prefix "#dev"
        dev_ch = _extract_channels(
            await server.call_tool("chat_list_channels", {"prefix": "#dev"}),
        )

        # Then: only #dev-* channels returned
        dev_names = {str(c.get("name", "")) for c in dev_ch}
        assert dev_names == {"#dev-auth", "#dev-api"}

        # Then: each channel has correct message_count and last_activity_at
        for ch in all_ch:
            assert ch.get("message_count") == 1
            assert ch.get("last_activity_at") is not None

    async def test_summarize_stats_fallback(self, server: AgoraServer) -> None:
        """Summarize without LLM returns stats with correct counts."""
        # Given: 5 messages from 2 different agents
        for i in range(3):
            await server.call_tool("chat_post_message", {
                "channel": "#stats",
                "content": f"msg {i}",
                "_agent_id": "agent-alpha",
            })
        for i in range(2):
            await server.call_tool("chat_post_message", {
                "channel": "#stats",
                "content": f"msg {i + 3}",
                "_agent_id": "agent-beta",
            })

        # When: summarizing with no LLM configured
        result = await server.call_tool("chat_summarize_channel", {
            "channel": "#stats",
        })

        # Then: stats contain expected fields
        assert result.get("message_count") == 5
        assert isinstance(result.get("participants"), int)
        assert int(str(result.get("participants", 0))) >= 2
        assert isinstance(result.get("time_span_hours"), (int, float))
        assert float(str(result.get("time_span_hours", 0))) >= 0
        summary = str(result.get("summary", ""))
        assert "5" in summary  # message count mentioned

    async def test_summarize_built_in_stub(self, llm_stub_server: AgoraServer) -> None:
        """Built-in LLM returns 'not yet implemented' stub with correct count."""
        # Given: 3 messages posted
        for i in range(3):
            await llm_stub_server.call_tool("chat_post_message", {
                "channel": "#stub-test",
                "content": f"message {i}",
            })

        # When: summarizing with built-in LLM
        result = await llm_stub_server.call_tool("chat_summarize_channel", {
            "channel": "#stub-test",
        })

        # Then: stub text and correct message_count
        summary = str(result.get("summary", ""))
        assert "not yet implemented" in summary
        assert result.get("message_count") == 3

    async def test_summarize_custom_llm(self, server: AgoraServer) -> None:
        """Summarize with a patched LLM endpoint returns its response."""
        # Given: 2 messages posted
        for i in range(2):
            await server.call_tool("chat_post_message", {
                "channel": "#llm-test",
                "content": f"content {i}",
            })

        # When: LLM endpoint is configured and patched
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"choices":[{"message":{"content":"Mock summary"}}]}'
        )
        plugin = cast("ChatPlugin", server.plugins[0])
        plugin.llm_api_url = "https://fake-llm.example.com/v1/chat/completions"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = await server.call_tool("chat_summarize_channel", {
                "channel": "#llm-test",
            })

        # Then: summary contains the LLM response
        assert "Mock summary" in str(result.get("summary", ""))
        assert result.get("message_count") == 2

    async def test_event_hooks_agent_join_leave(self, server: AgoraServer) -> None:
        """Agent register/disconnect posts joined/left messages to #general."""
        # Given: 2 agents registered sequentially
        reg_a = await server.call_tool("register", {"name": "alpha"})
        agent_a = str(reg_a.get("agent_id", ""))
        reg_b = await server.call_tool("register", {"name": "bravo"})
        agent_b = str(reg_b.get("agent_id", ""))

        # When: reading #general after registration
        read = await server.call_tool("chat_read_messages", {"channel": "#general"})
        messages = _extract_messages(read)

        # Then: "joined" messages for both agents present
        joined_a = [
            m for m in messages
            if "joined" in str(m.get("content", "")) and agent_a in str(m.get("content", ""))
        ]
        joined_b = [
            m for m in messages
            if "joined" in str(m.get("content", "")) and agent_b in str(m.get("content", ""))
        ]
        assert len(joined_a) >= 1
        assert len(joined_b) >= 1

        # When: emitting disconnect events
        evt_bus = server._eventbus  # noqa: SLF001  # white-box test
        assert evt_bus is not None
        await evt_bus.emit("agent.disconnected", agent_id=agent_a)
        await evt_bus.emit("agent.disconnected", agent_id=agent_b)

        # Then: "left" messages present, total = 4 (2 joins + 2 leaves)
        read2 = await server.call_tool("chat_read_messages", {"channel": "#general"})
        all_msgs = _extract_messages(read2)
        left_msgs = [
            m for m in all_msgs
            if "left" in str(m.get("content", ""))
        ]
        assert len(left_msgs) >= 2
        assert len(all_msgs) >= 4

    async def test_channel_limit_enforced(self, server: AgoraServer) -> None:
        """Posting to more than max_channels channels returns CHANNEL_LIMIT."""
        # Given: max_channels=10, post to 10 channels (all succeed)
        for i in range(10):
            result = await server.call_tool("chat_post_message", {
                "channel": f"#limit-{i}",
                "content": f"hello {i}",
            })
            assert "message_id" in result

        # When: posting to the 11th channel
        overflow = await server.call_tool("chat_post_message", {
            "channel": "#overflow",
            "content": "should fail",
        })

        # Then: CHANNEL_LIMIT error returned
        assert overflow.get("error") == "CHANNEL_LIMIT"

    async def test_concurrent_posts(self, server: AgoraServer) -> None:
        """10 concurrent posts to the same channel all succeed."""
        # Given/When: 10 concurrent posts
        results = await asyncio.gather(*[
            server.call_tool("chat_post_message", {
                "channel": "#concurrent",
                "content": f"post-{i}",
            })
            for i in range(10)
        ], return_exceptions=True)

        # Then: all succeeded
        for r in results:
            assert isinstance(r, dict), f"Expected dict, got {type(r)}"
            assert "message_id" in r

        # Then: all 10 messages present
        read = await server.call_tool("chat_read_messages", {"channel": "#concurrent"})
        messages = _extract_messages(read)
        assert len(messages) == 10

    async def test_validation_errors(self, server: AgoraServer) -> None:
        """All validation error paths return correct error codes."""
        # Given/When/Then: empty content
        r1 = await server.call_tool("chat_post_message", {
            "channel": "#val", "content": "",
        })
        assert r1.get("error") == "VALIDATION_ERROR"

        # Empty channel name
        r2 = await server.call_tool("chat_post_message", {
            "channel": "", "content": "hello",
        })
        assert r2.get("error") == "VALIDATION_ERROR"

        # Missing channel parameter
        r3 = await server.call_tool("chat_post_message", {"content": "hello"})
        assert r3.get("error") == "VALIDATION_ERROR"

        # Content > max_message_length
        r4 = await server.call_tool("chat_post_message", {
            "channel": "#val", "content": "x" * 100_001,
        })
        assert r4.get("error") == "VALIDATION_ERROR"

        # Invalid order parameter
        r5 = await server.call_tool("chat_read_messages", {
            "channel": "#val", "order": "random",
        })
        assert r5.get("error") == "VALIDATION_ERROR"

        # Summarize non-existent channel
        r6 = await server.call_tool("chat_summarize_channel", {
            "channel": "#does-not-exist",
        })
        assert r6.get("error") == "CHANNEL_NOT_FOUND"

        # Limit > 1000
        r7 = await server.call_tool("chat_read_messages", {
            "channel": "#val", "limit": 1001,
        })
        assert r7.get("error") == "VALIDATION_ERROR"

    async def test_message_persistence_and_ordering(
        self, server: AgoraServer,
    ) -> None:
        """Messages persist and respect order, limit, and since filters."""
        # Given: 5 messages posted in sequence
        timestamps: list[str] = []
        for i in range(5):
            res = await server.call_tool("chat_post_message", {
                "channel": "#ordering",
                "content": f"Message {i}",
            })
            timestamps.append(str(res.get("created_at", "")))

        # When: reading asc (default)
        asc = _extract_messages(
            await server.call_tool("chat_read_messages", {"channel": "#ordering"}),
        )
        assert [m.get("content") for m in asc] == [
            "Message 0", "Message 1", "Message 2", "Message 3", "Message 4",
        ]

        # When: reading desc
        desc = _extract_messages(
            await server.call_tool("chat_read_messages", {
                "channel": "#ordering", "order": "desc",
            }),
        )
        assert desc[0].get("content") == "Message 4"
        assert desc[-1].get("content") == "Message 0"

        # When: reading with limit=2
        limited = _extract_messages(
            await server.call_tool("chat_read_messages", {
                "channel": "#ordering", "limit": 2,
            }),
        )
        assert len(limited) == 2

        # When: reading with since=timestamp of message 3
        since_msgs = _extract_messages(
            await server.call_tool("chat_read_messages", {
                "channel": "#ordering", "since": timestamps[3],
            }),
        )
        assert all(
            m.get("content") in ("Message 3", "Message 4") for m in since_msgs
        )

    async def test_long_channel_name_and_content(self, server: AgoraServer) -> None:
        """Long channel names and boundary-length content work correctly."""
        # Given: a channel name with 200+ characters
        long_name = "#" + "x" * 200
        result = await server.call_tool("chat_post_message", {
            "channel": long_name, "content": "works",
        })
        assert "message_id" in result

        # When: content at exactly max_message_length
        exact = await server.call_tool("chat_post_message", {
            "channel": "#exact", "content": "a" * 100_000,
        })
        assert "message_id" in exact

        # When: content at max_message_length + 1
        over = await server.call_tool("chat_post_message", {
            "channel": "#exact", "content": "b" * 100_001,
        })
        assert over.get("error") == "VALIDATION_ERROR"

    async def test_concurrent_read_while_write(self, server: AgoraServer) -> None:
        """Concurrent reads and writes to the same channel all succeed."""
        # Given: 5 initial messages
        for i in range(5):
            await server.call_tool("chat_post_message", {
                "channel": "#rw-concurrent",
                "content": f"initial-{i}",
            })

        # When: 2 reads + 2 writes concurrently
        tasks = [
            server.call_tool("chat_read_messages", {"channel": "#rw-concurrent"}),
            server.call_tool("chat_read_messages", {"channel": "#rw-concurrent"}),
            server.call_tool("chat_post_message", {
                "channel": "#rw-concurrent", "content": "write-1",
            }),
            server.call_tool("chat_post_message", {
                "channel": "#rw-concurrent", "content": "write-2",
            }),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Then: all returned successfully (no exceptions)
        dicts = [r for r in results if isinstance(r, dict)]
        assert len(dicts) == 4
        for d in dicts:
            assert "message_id" in d or "messages" in d

        # Then: reads see >= 5 messages (consistent snapshot)
        for d in dicts[:2]:
            messages = _extract_messages(d)
            assert len(messages) >= 5
