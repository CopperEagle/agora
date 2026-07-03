"""Backbone base classes for The Agora.

Provides ToolDef (tool definition dataclass) and AgoraPlugin (base class
that all plugins subclass).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agora.backbone.database import Database
    from agora.backbone.eventbus import EventBus


ToolHandler = Callable[..., Awaitable[dict[str, object]]]  # type: ignore[explicit-any]


@dataclass(frozen=True, slots=True)
class ToolDef:
    """Definition of a tool provided by a plugin.

    Attributes:
        name: Tool name (will be prefixed by plugin namespace,
              e.g. 'chat_post_message').
        handler: Async callable that implements the tool logic.
        description: Human-readable description for MCP schema.
    """

    name: str
    handler: ToolHandler
    description: str = ""


class AgoraPlugin:
    """Base class for all Agora plugins.

    Subclass and override the methods your plugin needs. All lifecycle
    hooks have default no-op implementations — only override what you use.

    Attributes:
        name: Unique plugin name (e.g. 'chat', 'board').
        version: Semver string.
        description: Human-readable description.
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    database: Database | None = None
    eventbus: EventBus | None = None

    async def on_load(self, config: dict[str, object]) -> None:
        """Called when the plugin is loaded. Parse and validate config.

        Args:
            config: Plugin-specific configuration dict from server config.
        """

    async def on_startup(self) -> None:
        """Called after migrations complete. Initialize connections, caches."""

    async def on_shutdown(self) -> None:
        """Called during server shutdown. Clean up resources (5 s timeout)."""

    async def on_agent_register(self, agent_id: str) -> None:
        """Called when a new agent registers with the server.

        Args:
            agent_id: The UUID of the registered agent.
        """

    async def on_agent_disconnect(self, agent_id: str) -> None:
        """Called when an agent disconnects or heartbeats expire.

        Args:
            agent_id: The UUID of the disconnected agent.
        """

    def get_tools(self) -> list[ToolDef]:
        """Return the tools this plugin provides.

        Returns:
            List of ToolDef instances. Empty list if plugin provides no tools.
        """
        return []

    def get_migrations(self) -> list[str]:
        """Return SQL migration statements in execution order.

        Returns:
            List of SQL strings. Empty list if plugin needs no tables.
        """
        return []
