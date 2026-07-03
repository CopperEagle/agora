"""Tests for the in-process async EventBus."""

from __future__ import annotations

import inspect

from agora.backbone.eventbus import EventBus


async def test_subscribe_and_emit() -> None:
    """Given a handler subscribed to 'test.event', When emit fires with data,
    Then the handler receives the payload."""
    bus = EventBus()
    received: list[dict[str, object]] = []

    async def handler(event_name: str, **data: object) -> None:
        _ = event_name  # tested in test_subscriber_receives_event_name
        received.append(data)

    bus.subscribe("test.event", handler)
    await bus.emit("test.event", foo="bar", count=42)

    assert len(received) == 1
    assert received[0] == {"foo": "bar", "count": 42}


async def test_multiple_subscribers() -> None:
    """Given two handlers subscribed to the same event, When emit fires,
    Then both handlers receive the payload."""
    bus = EventBus()
    results_a: list[str] = []
    results_b: list[str] = []

    async def handler_a(**_data: object) -> None:
        results_a.append("a")

    async def handler_b(**_data: object) -> None:
        results_b.append("b")

    bus.subscribe("shared.event", handler_a)
    bus.subscribe("shared.event", handler_b)
    await bus.emit("shared.event", msg="hello")

    assert results_a == ["a"]
    assert results_b == ["b"]


async def test_unsubscribe() -> None:
    """Given a subscribed handler, When unsubscribed before emit,
    Then the handler is not called."""
    bus = EventBus()
    calls: list[str] = []

    async def handler(**_data: object) -> None:
        calls.append("called")

    bus.subscribe("test.event", handler)
    bus.unsubscribe("test.event", handler)
    await bus.emit("test.event")

    assert calls == []


async def test_handler_crash_isolation() -> None:
    """Given two handlers where one raises, When emit fires,
    Then the non-crashing handler still receives the event."""
    bus = EventBus()
    good_handler_calls: list[str] = []

    async def crashing_handler(**_data: object) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    async def good_handler(**_data: object) -> None:
        good_handler_calls.append("ok")

    bus.subscribe("test.event", crashing_handler)
    bus.subscribe("test.event", good_handler)
    await bus.emit("test.event")

    assert good_handler_calls == ["ok"]


async def test_no_subscribers() -> None:
    """Given no subscribers, When emit fires, Then no error is raised."""
    bus = EventBus()
    await bus.emit("nonexistent.event", data="safe")


async def test_emit_is_async() -> None:
    """Given the EventBus, When inspecting emit, Then it returns an awaitable."""
    bus = EventBus()
    coro = bus.emit("test.event")
    assert inspect.isawaitable(coro)
    coro.close()


async def test_subscriber_receives_event_name() -> None:
    """Given a handler subscribed to an event, When emit fires,
    Then the handler receives the event name as a keyword argument."""
    bus = EventBus()
    received_events: list[str] = []

    async def handler(event_name: str, **_data: object) -> None:
        received_events.append(event_name)

    bus.subscribe("agent.registered", handler)
    bus.subscribe("tool.executed", handler)
    await bus.emit("agent.registered", agent_id="abc")
    await bus.emit("tool.executed", tool="chat_post")

    assert received_events == ["agent.registered", "tool.executed"]
