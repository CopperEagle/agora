"""Agent registry — register, heartbeat, status, and discovery.

Maintains the ``agents`` table in the shared SQLite database.  All public
methods are async because they delegate to the ``Database`` layer.

Example::

    reg = AgentRegistry(database=db, eventbus=bus)
    await reg.initialize()
    agent_id = await reg.register(name="alice", role="scout")
    await reg.heartbeat(agent_id)
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus

_MIGRATIONS: list[str] = [
    """\
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    capabilities TEXT,
    manifest TEXT,
    current_task TEXT,
    last_heartbeat_at TEXT,
    registered_at TEXT NOT NULL
)""",
]




def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _row_to_agent(row: dict[str, object]) -> dict[str, object]:
    """Convert a database row dict into the public agent dict format.

    Deserializes the ``capabilities`` (JSON array) and ``manifest``
    (JSON object) columns into native Python types.

    Args:
        row: Raw row from the ``agents`` table.

    Returns:
        Agent dict with deserialized fields.

    """
    caps_raw = row.get("capabilities")
    manifest_raw = row.get("manifest")
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row.get("role"),
        "status": row["status"],
        "capabilities": json.loads(caps_raw) if caps_raw else [],  # type: ignore[arg-type]
        "manifest": json.loads(manifest_raw) if manifest_raw else {},  # type: ignore[arg-type]
        "current_task": row.get("current_task"),
        "last_heartbeat_at": row.get("last_heartbeat_at"),
        "registered_at": row["registered_at"],
    }


class AgentRegistry:
    """Register, heartbeat, and discover agents.

    Coordinates with the ``Database`` for persistence and the ``EventBus``
    for cross-plugin notifications.

    Attributes:
        _database: Shared async database connection.

        _eventbus: In-process event bus for agent lifecycle events.

    """

    def __init__(self, database: Database, eventbus: EventBus) -> None:
        """Initialize the registry with shared infrastructure.

        Args:
            database: Connected async database instance.

            eventbus: In-process event bus for lifecycle events.

        """
        self._database = database
        self._eventbus = eventbus

    @property
    def eventbus(self) -> EventBus:
        """Return the event bus instance for external subscribers."""
        return self._eventbus

    async def initialize(self) -> None:
        """Run migrations to create the ``agents`` table if absent."""
        await self._database.run_migrations("backbone", _MIGRATIONS)

    async def register(
        self,
        name: str,
        role: str | None = None,
        capabilities: list[str] | None = None,
        manifest: dict[str, object] | None = None,
    ) -> str:
        """Register an agent with the backbone.

        If the name is already taken, re-registers by updating the existing
        agent's details and returning the same ``agent_id``.

        Args:
            name: Human-readable agent name.  Must be non-empty and unique.

            role: Optional role string (e.g. ``"reviewer"``, ``"scout"``).

            capabilities: Capability strings for discovery filtering.

            manifest: Free-form JSON metadata.

        Returns:
            Server-generated UUID4 string identifying the agent.

        Raises:
            ValueError: If *name* is empty.

        """
        if not name:
            msg = "Agent name must not be empty"
            raise ValueError(msg)

        existing = await self._database.execute(
            "SELECT id FROM agents WHERE name = ?",
            (name,),
        )

        if existing:
            agent_id: str = str(existing[0]["id"])
            await self._database.execute(
                "UPDATE agents SET role = ?, capabilities = ?, manifest = ? WHERE id = ?",
                (
                    role,
                    json.dumps(capabilities) if capabilities is not None else None,
                    json.dumps(manifest) if manifest is not None else None,
                    agent_id,
                ),
            )
        else:
            agent_id = str(uuid.uuid4())
            now = _now_iso()
            await self._database.execute(
                "INSERT INTO agents (id, name, role, status, capabilities, manifest, registered_at)"
                " VALUES (?, ?, ?, 'offline', ?, ?, ?)",
                (
                    agent_id,
                    name,
                    role,
                    json.dumps(capabilities) if capabilities is not None else None,
                    json.dumps(manifest) if manifest is not None else None,
                    now,
                ),
            )

        await self._eventbus.emit(
            "agent.registered",
            agent_id=agent_id,
            name=name,
            role=role,
        )
        return agent_id

    async def heartbeat(self, agent_id: str) -> None:
        """Update the last heartbeat timestamp for an agent.

        Args:
            agent_id: UUID string of the agent.

        Raises:
            ValueError: If no agent with the given id exists.

        """
        await self._require_agent(agent_id)
        await self._database.execute(
            "UPDATE agents SET last_heartbeat_at = ? WHERE id = ?",
            (_now_iso(), agent_id),
        )

    async def set_status(
        self,
        agent_id: str,
        status: str,
        task: str | None = None,
    ) -> None:
        """Update agent status and optional current task.

        Args:
            agent_id: UUID string of the agent.

            status: New status value (e.g. ``"online"``, ``"busy"``).

            task: Optional current task description.

        Raises:
            ValueError: If no agent with the given id exists.

        """
        await self._require_agent(agent_id)
        await self._database.execute(
            "UPDATE agents SET status = ?, current_task = ? WHERE id = ?",
            (status, task, agent_id),
        )

    async def list_agents(
        self,
        role: str | None = None,
        name_prefix: str | None = None,
    ) -> list[dict[str, object]]:
        """List agents with optional filtering by role and name prefix.

        Use this when discovering available teammates.  Supports
        filtering by role and name prefix for multi-team deployments.

        Args:
            role: Optional role filter (exact match, e.g. ``"reviewer"``).

            name_prefix: Optional name prefix filter (e.g. ``"team-alpha-"``).

        Returns:
            List of agent dicts in registration order.

        """
        query = "SELECT * FROM agents WHERE 1=1"
        params: list[str] = []
        if role is not None:
            query += " AND role = ?"
            params.append(role)
        if name_prefix is not None:
            query += " AND name LIKE ?"
            params.append(f"{name_prefix}%")
        query += " ORDER BY registered_at ASC"
        rows = await self._database.execute(query, tuple(params))
        return [_row_to_agent(r) for r in rows]

    async def find_agents(self, capability: str) -> list[dict[str, object]]:
        """Find agents whose capabilities include the given string.

        Performs a LIKE search against the JSON array stored in the
        ``capabilities`` column.

        Args:
            capability: Capability string to search for.

        Returns:
            List of matching agent dicts.

        """
        # LIKE match against the JSON array string, e.g. '["code","review"]'
        pattern = f'%"{capability}"%'
        rows = await self._database.execute(
            "SELECT * FROM agents WHERE capabilities LIKE ? ORDER BY registered_at",
            (pattern,),
        )
        return [_row_to_agent(r) for r in rows]

    async def get_agent(self, agent_id: str) -> dict[str, object] | None:
        """Retrieve an agent by its UUID.

        Args:
            agent_id: UUID string of the agent.

        Returns:
            Agent dict if found, ``None`` otherwise.

        """
        rows = await self._database.execute(
            "SELECT * FROM agents WHERE id = ?",
            (agent_id,),
        )
        return _row_to_agent(rows[0]) if rows else None

    async def get_agent_by_name(self, name: str) -> dict[str, object] | None:
        """Retrieve an agent by its unique name.

        Args:
            name: Human-readable agent name.

        Returns:
            Agent dict if found, ``None`` otherwise.

        """
        rows = await self._database.execute(
            "SELECT * FROM agents WHERE name = ?",
            (name,),
        )
        return _row_to_agent(rows[0]) if rows else None

    async def _require_agent(self, agent_id: str) -> None:
        """Raise ValueError if no agent with the given id exists.

        Args:
            agent_id: UUID string to look up.

        Raises:
            ValueError: If agent not found.

        """
        agent = await self.get_agent(agent_id)
        if agent is None:
            msg = f"No agent with id '{agent_id}'"
            raise ValueError(msg)
