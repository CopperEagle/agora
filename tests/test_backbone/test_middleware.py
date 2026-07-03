"""Tests for the AuthMiddleware — bridges RequestRouter auth with FastMCP transport."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
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


class _FakeContext:
    """Minimal stand-in for fastmcp.server.context.Context with session_id."""

    def __init__(self, session_id: str | None = None) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            msg = "session_id is not available"
            raise RuntimeError(msg)
        return self._session_id


async def _mock_call_next(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> ToolResult:
    """Mock call_next that returns a ToolResult echoing the tool name."""
    return ToolResult(content=[context.message.name])


def _make_context(
    tool_name: str = "test_tool",
    session_id: str | None = None,
) -> MiddlewareContext[mt.CallToolRequestParams]:
    """Create a MiddlewareContext for testing on_call_tool."""
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name=tool_name),
        fastmcp_context=_FakeContext(session_id=session_id) if session_id is not None else None,  # type: ignore[arg-type]
        method="tools/call",
    )


# ── Inheritance ────────────────────────────────────────────────────


async def test_middleware_inherits_from_middleware() -> None:
    """Given AuthMiddleware, When checking class hierarchy,
    Then it inherits from fastmcp Middleware."""
    assert issubclass(AuthMiddleware, Middleware)


# ── Constructor ────────────────────────────────────────────────────


async def test_auth_middleware_constructor(
    middleware: AuthMiddleware, registry: AgentRegistry,
) -> None:
    """Given a router, When creating AuthMiddleware,
    Then it stores the router reference for auth lookups."""
    # Verify the middleware delegates auth to the correct router by
    # confirming a registered agent passes and an unregistered one is blocked.
    agent_id = await registry.register(name="probe")
    ctx_allowed = _make_context(tool_name="echo", session_id=agent_id)
    result = await middleware.on_call_tool(ctx_allowed, _mock_call_next)
    assert isinstance(result, ToolResult)

    ctx_rejected = _make_context(tool_name="echo", session_id="no-such")
    with pytest.raises(ToolError, match="NOT_AUTHORIZED"):
        await middleware.on_call_tool(ctx_rejected, _mock_call_next)


# ── Registered agent allowed ───────────────────────────────────────


async def test_registered_agent_allowed(
    middleware: AuthMiddleware, registry: AgentRegistry,
) -> None:
    """Given a registered agent with valid session_id,
    When calling any tool through middleware,
    Then the call is allowed and passes through."""
    agent_id = await registry.register(name="alice")
    ctx = _make_context(tool_name="echo", session_id=agent_id)
    result = await middleware.on_call_tool(ctx, _mock_call_next)
    assert isinstance(result, ToolResult)


# ── Unregistered agent rejected ────────────────────────────────────


async def test_unregistered_agent_rejected(middleware: AuthMiddleware) -> None:
    """Given an unregistered session_id,
    When calling a non-register tool,
    Then ToolError is raised."""
    ctx = _make_context(tool_name="echo", session_id="unknown-session")
    with pytest.raises(ToolError, match="NOT_AUTHORIZED"):
        await middleware.on_call_tool(ctx, _mock_call_next)


async def test_no_session_rejected(middleware: AuthMiddleware) -> None:
    """Given no session context (fastmcp_context is None),
    When calling a non-register tool,
    Then ToolError is raised."""
    ctx = _make_context(tool_name="echo", session_id=None)
    with pytest.raises(ToolError, match="NOT_AUTHORIZED"):
        await middleware.on_call_tool(ctx, _mock_call_next)


# ── Register tool always allowed ───────────────────────────────────


async def test_register_tool_always_allowed(middleware: AuthMiddleware) -> None:
    """Given the 'register' tool,
    When called without any authentication,
    Then the call is allowed through."""
    ctx = _make_context(tool_name="register", session_id=None)
    result = await middleware.on_call_tool(ctx, _mock_call_next)
    assert isinstance(result, ToolResult)


# ── call_next invoked on success ───────────────────────────────────


async def test_middleware_calls_call_next_on_success(
    middleware: AuthMiddleware, registry: AgentRegistry,
) -> None:
    """Given a registered agent,
    When middleware auth passes,
    Then call_next is invoked and its result returned."""
    agent_id = await registry.register(name="bob")
    ctx = _make_context(tool_name="chat_echo", session_id=agent_id)

    call_next_called = False

    async def tracking_call_next(
        _context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        nonlocal call_next_called
        call_next_called = True
        return ToolResult(content=[f"handled:{_context.message.name}"])

    result = await middleware.on_call_tool(ctx, tracking_call_next)  # type: ignore[arg-type]
    assert call_next_called is True
    assert isinstance(result, ToolResult)


# ── call_next NOT invoked on rejection ─────────────────────────────


async def test_middleware_does_not_call_next_on_rejection(
    middleware: AuthMiddleware,
) -> None:
    """Given an unauthenticated caller,
    When middleware rejects,
    Then call_next is NOT invoked."""
    ctx = _make_context(tool_name="echo", session_id="bad-id")

    call_next_called = False

    async def tracking_call_next(
        _context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        nonlocal call_next_called
        call_next_called = True
        return ToolResult(content=["should not reach"])

    with pytest.raises(ToolError, match="NOT_AUTHORIZED"):
        await middleware.on_call_tool(ctx, tracking_call_next)  # type: ignore[arg-type]

    assert call_next_called is False


# ── Tool name extraction ───────────────────────────────────────────


async def test_tool_name_extracted_from_context(
    middleware: AuthMiddleware, registry: AgentRegistry,
) -> None:
    """Given a context with a specific tool name,
    When middleware processes it,
    Then the correct tool name is used for auth decision."""
    agent_id = await registry.register(name="carol")
    ctx = _make_context(tool_name="board_write", session_id=agent_id)
    result = await middleware.on_call_tool(ctx, _mock_call_next)
    assert isinstance(result, ToolResult)


# ── Unknown session_id rejected ────────────────────────────────────


async def test_unknown_session_id_rejected(middleware: AuthMiddleware) -> None:
    """Given a session_id that doesn't map to any agent,
    When calling a non-register tool,
    Then ToolError is raised."""
    ctx = _make_context(
        tool_name="chat_post",
        session_id="550e8400-e29b-41d4-a716-446655440000",
    )
    with pytest.raises(ToolError, match="NOT_AUTHORIZED"):
        await middleware.on_call_tool(ctx, _mock_call_next)
