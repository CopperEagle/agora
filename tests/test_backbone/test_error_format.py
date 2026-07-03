"""Tests for standardized 4-field error response format.

Every error response across the codebase must include exactly:
  - ``error``: Machine-readable error code (e.g. ``VALIDATION_ERROR``)
  - ``message``: Human-readable explanation
  - ``details``: Context dict (may be empty)
  - ``fix``: Actionable next step for the caller

This test suite verifies the 4-field contract for every error path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import pytest

from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus
from agora.backbone.registry import AgentRegistry
from agora.backbone.router import RequestRouter
from agora.backbone.server import AgoraServer

# Required keys in every error response.
_ERROR_KEYS = {"error", "message", "details", "fix"}


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a started AgoraServer with the chat plugin, in-memory DB."""
    srv = AgoraServer(
        config={
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
        },
        skip_transport=True,
    )
    await srv.start()
    yield srv
    await srv.stop()


# ── Helpers ────────────────────────────────────────────────────────


async def _echo_handler(*_args: object, **_kwargs: object) -> dict[str, object]:
    """Echo handler for router tests."""
    return {"ok": True}


# ── 1. chat_post_message VALIDATION_ERROR has all 4 keys ────────


async def test_post_message_empty_channel_has_all_keys(
    server: AgoraServer,
) -> None:
    """Given chat_post_message with empty channel, When posting,
    Then the VALIDATION_ERROR response has all 4 required keys."""
    result = await server.call_tool(
        "chat_post_message", {"channel": "", "content": "hi"},
    )
    assert "error" in result
    assert result["error"] == "VALIDATION_ERROR"
    assert _ERROR_KEYS.issubset(result.keys()), (
        f"Missing keys: {_ERROR_KEYS - result.keys()}"
    )


async def test_post_message_empty_content_has_all_keys(
    server: AgoraServer,
) -> None:
    """Given chat_post_message with empty content, When posting,
    Then the VALIDATION_ERROR response has all 4 required keys."""
    result = await server.call_tool(
        "chat_post_message", {"channel": "#test", "content": ""},
    )
    assert result["error"] == "VALIDATION_ERROR"
    assert _ERROR_KEYS.issubset(result.keys()), (
        f"Missing keys: {_ERROR_KEYS - result.keys()}"
    )


async def test_post_message_content_too_long_has_all_keys(
    server: AgoraServer,
) -> None:
    """Given chat_post_message with content exceeding max length,
    When posting, then VALIDATION_ERROR has all 4 keys."""
    # The default max is 100_000, so we send 100_001 chars
    long_content = "x" * 100_001
    result = await server.call_tool(
        "chat_post_message", {"channel": "#test", "content": long_content},
    )
    assert result["error"] == "VALIDATION_ERROR"
    assert _ERROR_KEYS.issubset(result.keys()), (
        f"Missing keys: {_ERROR_KEYS - result.keys()}"
    )


# ── 2. NOT_AUTHORIZED fix mentions "register" ────────────────────


async def test_not_authorized_fix_mentions_register() -> None:
    """Given a server without going through auth middleware, When route()
    encounters an unauthenticated call, then the fix mentions register."""
    db = Database(":memory:")
    await db.connect()
    try:
        bus = EventBus()
        reg = AgentRegistry(database=db, eventbus=bus)
        await reg.initialize()
        router = RequestRouter(registry=reg, eventbus=bus)
        router.register_tool("echo", _echo_handler)

        result = await router.route("echo", {}, session_id=None)
        assert result["error"] == "NOT_AUTHORIZED"
        assert "fix" in result
        assert "register" in result["fix"].lower()
    finally:
        await db.close()


# ── 3. TOOL_NOT_FOUND fix mentions tool name or available tools ───


async def test_tool_not_found_fix_lists_available_tools() -> None:
    """Given a router with registered tools, When routing an unknown tool,
    Then the fix field mentions available tools or 'Check tool name'."""
    db = Database(":memory:")
    await db.connect()
    try:
        bus = EventBus()
        reg = AgentRegistry(database=db, eventbus=bus)
        await reg.initialize()
        router = RequestRouter(registry=reg, eventbus=bus)
        router.register_tool("echo", _echo_handler)
        router.register_tool("ping", _echo_handler)

        agent_id = await reg.register(name="tester")
        result = await router.route("nonexistent_tool", {}, agent_id)
        assert result["error"] == "TOOL_NOT_FOUND"
        assert "fix" in result
        # fix should either list available tools or say "Check tool name"
        fix_lower = result["fix"].lower()
        assert "check tool name" in fix_lower or "echo" in fix_lower
    finally:
        await db.close()


# ── 4. VALIDATION_ERROR fix tells agent how to fix ───────────────


async def test_validation_error_empty_channel_fix_hint(
    server: AgoraServer,
) -> None:
    """Given chat_post_message with empty channel, When posting,
    Then the fix tells the agent to provide a non-empty channel name."""
    result = await server.call_tool(
        "chat_post_message", {"channel": "", "content": "hi"},
    )
    assert result["error"] == "VALIDATION_ERROR"
    fix = result["fix"].lower()
    assert "non-empty" in fix or "channel" in fix


async def test_validation_error_empty_content_fix_hint(
    server: AgoraServer,
) -> None:
    """Given chat_post_message with empty content, When posting,
    Then the fix tells the agent to provide non-empty content."""
    result = await server.call_tool(
        "chat_post_message", {"channel": "#test", "content": ""},
    )
    assert result["error"] == "VALIDATION_ERROR"
    fix = result["fix"].lower()
    assert "non-empty" in fix or "content" in fix


async def test_validation_error_invalid_limit_fix_hint(
    server: AgoraServer,
) -> None:
    """Given chat_read_messages with invalid limit, When reading,
    Then the fix tells the agent the valid range."""
    result = await server.call_tool(
        "chat_read_messages",
        {"channel": "#test", "limit": -1},
    )
    assert result["error"] == "VALIDATION_ERROR"
    fix = result["fix"].lower()
    assert "limit" in fix


async def test_validation_error_invalid_order_fix_hint(
    server: AgoraServer,
) -> None:
    """Given chat_read_messages with invalid order, When reading,
    Then the fix tells the agent to use 'asc' or 'desc'."""
    result = await server.call_tool(
        "chat_read_messages",
        {"channel": "#test", "order": "random"},
    )
    assert result["error"] == "VALIDATION_ERROR"
    fix = result["fix"].lower()
    assert "asc" in fix
    assert "desc" in fix


# ── 5. CHANNEL_NOT_FOUND fix mentions creating the channel ───────


async def test_channel_not_found_fix_mentions_creation(
    server: AgoraServer,
) -> None:
    """Given chat_summarize_channel with non-existent channel, When summarizing,
    Then the fix tells the agent to create the channel first."""
    result = await server.call_tool(
        "chat_summarize_channel",
        {"channel": "#nonexistent"},
    )
    assert result["error"] == "CHANNEL_NOT_FOUND"
    fix = result["fix"].lower()
    assert "create" in fix or "chat_post_message" in fix


# ── 6. Error responses are valid JSON (serializable) ─────────────


async def test_error_response_is_json_serializable(
    server: AgoraServer,
) -> None:
    """Given any error response from the server, When serialized to JSON,
    Then it produces valid JSON without errors."""
    errors = [
        await server.call_tool(
            "chat_post_message", {"channel": "", "content": "hi"},
        ),
        await server.call_tool(
            "chat_read_messages",
            {"channel": "#test", "order": "bad"},
        ),
        await server.call_tool(
            "chat_summarize_channel", {"channel": "#nope"},
        ),
    ]
    for error in errors:
        # Must be JSON-serializable without errors
        serialized = json.dumps(error)
        parsed = json.loads(serialized)
        assert isinstance(parsed, dict)
        assert _ERROR_KEYS.issubset(parsed.keys()), (
            f"Missing keys in serialized error: {_ERROR_KEYS - parsed.keys()}"
        )


async def test_error_response_details_is_dict() -> None:
    """Given any error response, When checking details, it's always a dict."""
    db = Database(":memory:")
    await db.connect()
    try:
        bus = EventBus()
        reg = AgentRegistry(database=db, eventbus=bus)
        await reg.initialize()
        router = RequestRouter(registry=reg, eventbus=bus)
        router.register_tool("echo", _echo_handler)

        # NOT_AUTHORIZED
        result = await router.route("echo", {}, session_id=None)
        assert isinstance(result["details"], dict)

        # TOOL_NOT_FOUND
        agent_id = await reg.register(name="tester")
        result = await router.route("nonexistent", {}, agent_id)
        assert isinstance(result["details"], dict)
    finally:
        await db.close()


async def test_tool_not_found_error_from_server_is_json_serializable(
    server: AgoraServer,
) -> None:
    """Given call_tool with unknown tool name, When serialized,
    Then it produces valid JSON with all 4 keys."""
    result = await server.call_tool("nonexistent_tool_xyz", {})
    assert result["error"] == "TOOL_NOT_FOUND"
    serialized = json.dumps(result)
    parsed = json.loads(serialized)
    assert _ERROR_KEYS.issubset(parsed.keys())


# ── CHANNEL_LIMIT fix ────────────────────────────────────────────


async def test_channel_limit_fix_mentions_config(
    server: AgoraServer,
) -> None:
    """Given chat_post_message when channel limit is reached,
    Then the fix suggests reducing channels or increasing config."""
    # Set max_channels to 1, create a channel, then try to create another
    for plugin in server.plugins:
        if plugin.name == "chat":
            plugin.max_channels = 1  # type: ignore[attr-defined]

    # First post creates a channel
    result1 = await server.call_tool(
        "chat_post_message", {"channel": "#ch1", "content": "first"},
    )
    assert "message_id" in result1

    # Second post should hit the limit
    result2 = await server.call_tool(
        "chat_post_message", {"channel": "#ch2", "content": "second"},
    )
    assert result2["error"] == "CHANNEL_LIMIT"
    assert "fix" in result2
    fix = result2["fix"].lower()
    assert "reduce" in fix or "increase" in fix or "config" in fix
