"""Tests for AuthMiddleware with argument-based auth.

The middleware extracts ``_agent_id`` from tool call arguments rather
than from ``fastmcp_ctx.session_id``, making auth transport-agnostic
(works over stdio, SSE, and Streamable HTTP).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult

from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus
from agora.backbone.middleware import AuthMiddleware
from agora.backbone.registry import AgentRegistry
from agora.backbone.router import RequestRouter

# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
async def eventbus() -> EventBus:
    """Provide a fresh EventBus per test."""
    return EventBus()


@pytest.fixture
async def database() -> AsyncGenerator[Database, None]:
    """Provide an in-memory Database connected and ready."""
    db = Database(":memory:")
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def registry(
    database: Database, eventbus: EventBus,
) -> AgentRegistry:
    """Provide an initialized AgentRegistry backed by in-memory Database."""
    reg = AgentRegistry(database=database, eventbus=eventbus)
    await reg.initialize()
    return reg


@pytest.fixture
async def router(
    registry: AgentRegistry, eventbus: EventBus,
) -> RequestRouter:
    """Provide a fresh RequestRouter wired to registry and eventbus."""
    return RequestRouter(registry=registry, eventbus=eventbus)


@pytest.fixture
async def middleware(router: RequestRouter) -> AuthMiddleware:
    """Provide an AuthMiddleware wired to the router."""
    return AuthMiddleware(router)


# ── Helpers ────────────────────────────────────────────────────────


class _FakeFastMCPContext:
    """Minimal fastmcp context with session_id (which should NOT be used for auth)."""

    def __init__(self, session_id: str | None = None) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            msg = "session_id is not available"
            raise RuntimeError(msg)
        return self._session_id


def _make_context(
    tool_name: str = "test_tool",
    arguments: dict[str, object] | None = None,
    session_id: str = "random-stdio-session-id",
) -> MiddlewareContext[mt.CallToolRequestParams]:
    """Create a MiddlewareContext for testing on_call_tool.

    Args:
        tool_name: Name of the tool being called.

        arguments: Tool call arguments (may include ``_agent_id``).

        session_id: Fake session ID to simulate MCP transport.

    Returns:
        A MiddlewareContext wired to a fake FastMCP context.

    """
    args = arguments or {}
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name=tool_name, arguments=args),
        fastmcp_context=_FakeFastMCPContext(session_id=session_id),
        method="tools/call",
    )


async def _mock_call_next(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> ToolResult:
    """Mock call_next that echoes the arguments as JSON."""
    return ToolResult(content=[json.dumps(context.message.arguments)])


# ── Tests ──────────────────────────────────────────────────────────


async def test_register_tool_without_agent_id_passes(
    middleware: AuthMiddleware,
) -> None:
    """Given the 'register' tool called without any _agent_id,
    When middleware processes it,
    Then the call is allowed through (register is unauthenticated)."""
    ctx = _make_context(tool_name="register", arguments={})
    result = await middleware.on_call_tool(ctx, _mock_call_next)
    assert isinstance(result, ToolResult)


async def test_non_register_tool_without_agent_id_rejected(
    middleware: AuthMiddleware,
) -> None:
    """Given a non-register tool called without _agent_id,
    When middleware processes it,
    Then ToolError is raised with structured error including fix field."""
    ctx = _make_context(tool_name="chat_post_message", arguments={})
    with pytest.raises(ToolError) as exc_info:
        await middleware.on_call_tool(ctx, _mock_call_next)

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"] == "NOT_AUTHORIZED"
    assert "fix" in error_data
    assert "register" in error_data["fix"].lower()


async def test_tool_with_valid_agent_id_succeeds(
    middleware: AuthMiddleware, registry: AgentRegistry,
) -> None:
    """Given a registered agent with valid _agent_id,
    When calling a non-register tool,
    Then the call passes through and handler receives the agent_id."""
    agent_id = await registry.register(name="alice")
    ctx = _make_context(
        tool_name="chat_echo", arguments={"_agent_id": agent_id},
    )

    captured_args: dict[str, object] = {}

    async def capturing_call_next(
        _context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        captured_args.update(_context.message.arguments or {})
        return ToolResult(content=["ok"])

    result = await middleware.on_call_tool(ctx, capturing_call_next)  # type: ignore[arg-type]
    assert isinstance(result, ToolResult)
    # Verify the agent_id was validated and injected into arguments.
    assert captured_args["_agent_id"] == agent_id


async def test_tool_with_invalid_agent_id_rejected(
    middleware: AuthMiddleware,
) -> None:
    """Given a non-existent _agent_id,
    When calling a non-register tool,
    Then ToolError is raised."""
    ctx = _make_context(
        tool_name="echo", arguments={"_agent_id": "nonexistent-agent-id"},
    )
    with pytest.raises(ToolError, match="NOT_AUTHORIZED"):
        await middleware.on_call_tool(ctx, _mock_call_next)


async def test_session_id_is_not_used_for_auth(
    middleware: AuthMiddleware, registry: AgentRegistry,
) -> None:
    """Given a valid _agent_id in arguments but a DIFFERENT session_id,
    When middleware processes it,
    Then auth succeeds (reads from arguments, not session_id).

    This is the key regression test — fastmcp_ctx.session_id is a random
    UUID for stdio transport and must never be used for auth.
    """
    agent_id = await registry.register(name="carol")
    ctx = _make_context(
        tool_name="chat_post_message",
        arguments={"_agent_id": agent_id},
        session_id="completely-different-stdio-uuid",
    )
    result = await middleware.on_call_tool(ctx, _mock_call_next)
    assert isinstance(result, ToolResult)


async def test_register_tool_with_agent_id_but_no_registration(
    middleware: AuthMiddleware,
) -> None:
    """Given the 'register' tool with a bogus _agent_id,
    When middleware processes it,
    Then the call is allowed through (register is always allowed)."""
    ctx = _make_context(
        tool_name="register",
        arguments={"_agent_id": "some-bogus-id"},
    )
    result = await middleware.on_call_tool(ctx, _mock_call_next)
    assert isinstance(result, ToolResult)
