"""Tests for the AgentRegistry — register, heartbeat, discovery."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus
from agora.backbone.registry import AgentRegistry


@pytest.fixture
async def registry() -> AsyncGenerator[AgentRegistry, None]:
    """Provide a fresh AgentRegistry backed by in-memory Database and EventBus."""
    database = Database(":memory:")
    await database.connect()
    bus = EventBus()
    reg = AgentRegistry(database=database, eventbus=bus)
    await reg.initialize()
    yield reg
    await database.close()


# ---------- Register ----------


async def test_register_returns_uuid(registry: AgentRegistry) -> None:
    """Given an empty registry, When registering an agent,
    Then a UUID string is returned."""
    agent_id = await registry.register(name="alice")
    assert isinstance(agent_id, str)
    assert len(agent_id) == 36  # UUID4 format


async def test_re_register_same_name(registry: AgentRegistry) -> None:
    """Given an agent named 'alice', When re-registering with the same name,
    Then the same agent_id is returned."""
    first_id = await registry.register(name="alice")
    second_id = await registry.register(name="alice")
    assert first_id == second_id


async def test_register_sets_default_status(registry: AgentRegistry) -> None:
    """Given a new agent, When registered,
    Then status defaults to 'offline'."""
    agent_id = await registry.register(name="bob")
    agent = await registry.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "offline"


async def test_register_empty_name(registry: AgentRegistry) -> None:
    """Given an empty name, When registering,
    Then ValueError is raised."""
    with pytest.raises(ValueError, match="name"):
        await registry.register(name="")


async def test_register_emits_event(registry: AgentRegistry) -> None:
    """Given a subscriber on 'agent.registered', When an agent registers,
    Then the event fires with correct data."""
    events: list[dict[str, object]] = []

    async def on_registered(event_name: str, **data: object) -> None:
        _ = event_name
        events.append(data)

    registry.eventbus.subscribe("agent.registered", on_registered)
    agent_id = await registry.register(name="carol", role="scout")
    assert len(events) == 1
    assert events[0]["agent_id"] == agent_id
    assert events[0]["name"] == "carol"
    assert events[0]["role"] == "scout"


# ---------- Heartbeat ----------


async def test_heartbeat_refreshes(registry: AgentRegistry) -> None:
    """Given a registered agent, When heartbeat is called,
    Then last_heartbeat_at is updated."""
    agent_id = await registry.register(name="dave")
    agent_before = await registry.get_agent(agent_id)
    assert agent_before is not None
    assert agent_before["last_heartbeat_at"] is None

    await registry.heartbeat(agent_id)
    agent_after = await registry.get_agent(agent_id)
    assert agent_after is not None
    assert agent_after["last_heartbeat_at"] is not None


async def test_heartbeat_unknown_agent(registry: AgentRegistry) -> None:
    """Given an unknown agent_id, When heartbeat is called,
    Then ValueError is raised."""
    with pytest.raises(ValueError, match="agent"):
        await registry.heartbeat("nonexistent-id")


# ---------- Set status ----------


async def test_set_status_updates(registry: AgentRegistry) -> None:
    """Given a registered agent, When set_status is called,
    Then the status field is updated."""
    agent_id = await registry.register(name="eve")
    await registry.set_status(agent_id, "busy")
    agent = await registry.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "busy"


async def test_set_status_with_task(registry: AgentRegistry) -> None:
    """Given a registered agent, When set_status includes a task,
    Then current_task is updated."""
    agent_id = await registry.register(name="frank")
    await registry.set_status(agent_id, "busy", task="review-pr")
    agent = await registry.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "busy"
    assert agent["current_task"] == "review-pr"


async def test_set_status_unknown_agent(registry: AgentRegistry) -> None:
    """Given an unknown agent_id, When set_status is called,
    Then ValueError is raised."""
    with pytest.raises(ValueError, match="agent"):
        await registry.set_status("nonexistent-id", "online")


# ---------- List agents ----------


async def test_list_agents(registry: AgentRegistry) -> None:
    """Given multiple registered agents, When listing without filter,
    Then all agents are returned."""
    await registry.register(name="alice")
    await registry.register(name="bob")
    agents = await registry.list_agents()
    names = {a["name"] for a in agents}
    assert names == {"alice", "bob"}


async def test_list_agents_with_filter(registry: AgentRegistry) -> None:
    """Given agents with different roles, When filtering by role,
    Then only matching agents are returned."""
    await registry.register(name="alice", role="scout")
    await registry.register(name="bob", role="builder")
    await registry.register(name="carol", role="scout")
    agents = await registry.list_agents({"role": "scout"})
    names = {a["name"] for a in agents}
    assert names == {"alice", "carol"}


async def test_list_agents_invalid_filter_column(registry: AgentRegistry) -> None:
    """Given a filter with an unknown column, When listing agents,
    Then ValueError is raised."""
    await registry.register(name="alice")
    with pytest.raises(ValueError, match="Invalid filter column"):
        await registry.list_agents({"nonexistent_column": "value"})


# ---------- Find agents by capability ----------


async def test_find_agents_by_capability(registry: AgentRegistry) -> None:
    """Given agents with various capabilities, When finding by capability,
    Then agents possessing it are returned."""
    await registry.register(name="alice", capabilities=["code", "review"])
    await registry.register(name="bob", capabilities=["docs"])
    await registry.register(name="carol", capabilities=["code", "deploy"])

    agents = await registry.find_agents("code")
    names = {a["name"] for a in agents}
    assert names == {"alice", "carol"}


async def test_find_agents_no_match(registry: AgentRegistry) -> None:
    """Given agents without a specific capability, When finding by it,
    Then an empty list is returned."""
    await registry.register(name="alice", capabilities=["docs"])
    agents = await registry.find_agents("deploy")
    assert agents == []


# ---------- Get agent ----------


async def test_get_agent_by_id(registry: AgentRegistry) -> None:
    """Given a registered agent, When getting by id,
    Then the correct agent dict is returned."""
    agent_id = await registry.register(name="alice", role="scout")
    agent = await registry.get_agent(agent_id)
    assert agent is not None
    assert agent["id"] == agent_id
    assert agent["name"] == "alice"
    assert agent["role"] == "scout"
    assert agent["capabilities"] == []
    assert agent["manifest"] == {}


async def test_get_agent_unknown_id(registry: AgentRegistry) -> None:
    """Given an unknown id, When getting by id,
    Then None is returned."""
    assert await registry.get_agent("no-such-id") is None


async def test_get_agent_by_name(registry: AgentRegistry) -> None:
    """Given a registered agent, When getting by name,
    Then the correct agent dict is returned."""
    agent_id = await registry.register(name="bob")
    agent = await registry.get_agent_by_name("bob")
    assert agent is not None
    assert agent["id"] == agent_id
    assert agent["name"] == "bob"


async def test_get_agent_by_name_unknown(registry: AgentRegistry) -> None:
    """Given an unknown name, When getting by name,
    Then None is returned."""
    assert await registry.get_agent_by_name("nobody") is None


# ---------- Full agent dict shape ----------


async def test_agent_dict_shape(registry: AgentRegistry) -> None:
    """Given a fully-populated agent, When retrieved,
    Then all fields are present with correct types."""
    agent_id = await registry.register(
        name="zara",
        role="lead",
        capabilities=["code", "review", "deploy"],
        manifest={"team": "core", "level": 5},
    )
    agent = await registry.get_agent(agent_id)
    assert agent is not None
    assert agent["id"] == agent_id
    assert agent["name"] == "zara"
    assert agent["role"] == "lead"
    assert agent["status"] == "offline"
    assert agent["capabilities"] == ["code", "review", "deploy"]
    assert agent["manifest"] == {"team": "core", "level": 5}
    assert agent["current_task"] is None
    assert agent["last_heartbeat_at"] is None
    assert isinstance(agent["registered_at"], str)
