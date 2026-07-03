"""Tests for the RequestRouter — authenticate, route, dispatch, audit."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus
from agora.backbone.registry import AgentRegistry
from agora.backbone.router import RequestRouter


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


# ── Helpers ────────────────────────────────────────────────────────


async def _echo_handler(*_: object, **kwargs: object) -> dict[str, object]:
    """Echo all keyword arguments back as-is."""
    return dict(kwargs)


async def _ok_handler(*_: object, **__: object) -> dict[str, object]:
    """Return a fixed success result."""
    return {"result": "ok"}


# ── Tool registration ──────────────────────────────────────────────


async def test_register_tool(router: RequestRouter) -> None:
    """Given a router, When registering a tool, Then it appears in list_tools."""
    router.register_tool("echo", _echo_handler)
    assert "echo" in router.list_tools()


async def test_register_tool_with_prefix(router: RequestRouter) -> None:
    """Given a router, When registering with a prefix,
    Then the tool name is prefixed."""
    router.register_tool("post_message", _ok_handler, prefix="chat")
    assert "chat_post_message" in router.list_tools()


async def test_duplicate_tool_name_raises(router: RequestRouter) -> None:
    """Given a registered tool, When registering the same name again,
    Then ValueError is raised."""
    router.register_tool("echo", _ok_handler)
    with pytest.raises(ValueError, match="Duplicate tool name"):
        router.register_tool("echo", _ok_handler)


async def test_list_tools(router: RequestRouter) -> None:
    """Given multiple tools, When listing, Then names are returned sorted."""
    router.register_tool("zebra", _ok_handler)
    router.register_tool("alpha", _ok_handler)
    router.register_tool("middle", _ok_handler)
    tools = router.list_tools()
    assert tools == ["alpha", "middle", "zebra"]


# ── Authentication ─────────────────────────────────────────────────


async def test_route_registered_agent(router: RequestRouter, registry: AgentRegistry) -> None:
    """Given a registered agent, When routing a tool call, Then it succeeds."""
    agent_id = await registry.register(name="alice")
    session_id = agent_id
    router.register_tool("echo", _echo_handler)
    result = await router.route("echo", {"msg": "hi"}, session_id)
    assert result["msg"] == "hi"


async def test_route_unregistered_agent_rejected(router: RequestRouter) -> None:
    """Given an unregistered session, When routing a non-register tool,
    Then PermissionError is raised."""
    router.register_tool("echo", _ok_handler)
    with pytest.raises(PermissionError, match="NOT_AUTHORIZED"):
        await router.route("echo", {}, "unknown-session")


async def test_route_unknown_tool(router: RequestRouter, registry: AgentRegistry) -> None:
    """Given a registered agent, When routing an unknown tool name,
    Then KeyError is raised."""
    agent_id = await registry.register(name="bob")
    with pytest.raises(KeyError, match="TOOL_NOT_FOUND"):
        await router.route("nonexistent_tool", {}, agent_id)


async def test_route_register_always_allowed(router: RequestRouter) -> None:
    """Given the 'register' tool is registered, When an unauthenticated session
    calls it, Then it is allowed."""
    router.register_tool("register", _ok_handler)
    result = await router.route("register", {}, None)
    assert result["result"] == "ok"


async def test_route_emits_executed_event(
    router: RequestRouter, registry: AgentRegistry, eventbus: EventBus,
) -> None:
    """Given a subscribed listener on 'tool.executed', When a tool is routed,
    Then the event is emitted with correct data."""
    agent_id = await registry.register(name="carol")
    events: list[dict[str, object]] = []

    async def on_tool_executed(event_name: str, **data: object) -> None:
        _ = event_name
        events.append(data)

    eventbus.subscribe("tool.executed", on_tool_executed)
    router.register_tool("echo", _echo_handler)
    await router.route("echo", {"key": "val"}, agent_id)

    assert len(events) == 1
    assert events[0]["tool"] == "echo"
    assert events[0]["agent_id"] == agent_id
    assert "result_keys" in events[0]


async def test_route_correct_handler_called(router: RequestRouter, registry: AgentRegistry) -> None:
    """Given two tools with different handlers, When routing,
    Then the correct handler is called."""
    agent_id = await registry.register(name="dave")

    async def handler_a(*_args: object, **_kw: object) -> dict[str, object]:
        return {"used": "a"}

    async def handler_b(*_args: object, **_kw: object) -> dict[str, object]:
        return {"used": "b"}

    router.register_tool("tool_a", handler_a)
    router.register_tool("tool_b", handler_b)

    result_a = await router.route("tool_a", {}, agent_id)
    result_b = await router.route("tool_b", {}, agent_id)
    assert result_a["used"] == "a"
    assert result_b["used"] == "b"


async def test_route_handler_receives_args(router: RequestRouter, registry: AgentRegistry) -> None:
    """Given a tool, When routing with specific args,
    Then the handler receives those exact kwargs."""
    agent_id = await registry.register(name="eve")
    received: list[dict[str, object]] = []

    async def capture_handler(*_: object, **kwargs: object) -> dict[str, object]:
        received.append(kwargs)
        return {"captured": True}

    router.register_tool("capture", capture_handler)
    await router.route("capture", {"x": 1, "y": "two"}, agent_id)

    assert len(received) == 1
    assert received[0] == {"x": 1, "y": "two"}


async def test_route_handler_result_returned(
    router: RequestRouter, registry: AgentRegistry,
) -> None:
    """Given a tool whose handler returns data, When routed,
    Then the handler's return dict is the route() return value."""
    agent_id = await registry.register(name="frank")

    async def data_handler(*_: object, **_kw: object) -> dict[str, object]:
        return {"status": "done", "count": 42}

    router.register_tool("data", data_handler)
    result = await router.route("data", {}, agent_id)
    assert result == {"status": "done", "count": 42}


async def test_authenticate_returns_agent_id(
    router: RequestRouter, registry: AgentRegistry,
) -> None:
    """Given a registered agent, When authenticating with session_id,
    Then the agent_id is returned."""
    agent_id = await registry.register(name="grace")
    result = await router.authenticate(agent_id)
    assert result == agent_id


async def test_authenticate_returns_none_for_unknown(router: RequestRouter) -> None:
    """Given an unregistered session, When authenticating,
    Then None is returned."""
    result = await router.authenticate("no-such-session")
    assert result is None


async def test_authenticate_returns_none_for_none(router: RequestRouter) -> None:
    """Given None session, When authenticating,
    Then None is returned."""
    result = await router.authenticate(None)
    assert result is None
