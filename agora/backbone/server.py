"""AgoraServer — assemble all backbone components into a running MCP server.

The server wires Database, EventBus, AgentRegistry, RequestRouter,
PluginLoader, AuthMiddleware, and FastMCP together.  It owns the full
lifecycle: start → serve → stop.

Example::

    server = AgoraServer(config={"db_path": "agora.db", "plugins": []})
    await server.start()
    await server.call_tool("register", {"name": "alice"})
    await server.stop()
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import cast

from fastmcp import FastMCP
from fastmcp.tools.base import Tool

from agora.backbone import AgoraPlugin
from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus
from agora.backbone.loader import PluginLoader
from agora.backbone.middleware import AuthMiddleware
from agora.backbone.registry import AgentRegistry
from agora.backbone.router import RequestRouter

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "agora.db"

_McpWrapper = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


def _make_mcp_wrapper(
    router: RequestRouter, tool_name: str,
) -> _McpWrapper:
    """Create an explicit-parameter wrapper for a router tool.

    FastMCP rejects functions with ``**kwargs``, so each tool needs a
    wrapper with named parameters.  The wrapper delegates to
    ``router.route()`` for dispatch and audit.

    Args:
        router: The RequestRouter containing the tool handler.

        tool_name: The fully-qualified tool name.

    Returns:
        An async callable suitable for ``Tool.from_function``.

    """

    async def _wrapper(arguments: dict[str, object]) -> dict[str, object]:
        return await router.route(tool_name, arguments, session_id=None)

    return _wrapper


class AgoraServer:
    """Assemble all backbone components and manage the server lifecycle.

    Creates Database, EventBus, AgentRegistry, RequestRouter, PluginLoader,
    and FastMCP server.  Registers backbone and plugin tools on the router
    and exposes ``call_tool`` for testing without transport.

    Attributes:
        config: The server configuration dict.

        skip_transport: Whether to skip stdio transport (for testing).

    """

    def __init__(
        self, config: dict[str, object], skip_transport: bool = False,
    ) -> None:
        """Initialize the server with a config dict.

        Args:
            config: Server configuration with ``db_path`` and ``plugins`` keys.

            skip_transport: When True, do not start stdio transport.

        """
        self._config: dict[str, object] = config
        self._skip_transport = skip_transport

        self._database: Database | None = None
        self._eventbus: EventBus | None = None
        self._registry: AgentRegistry | None = None
        self._router: RequestRouter | None = None
        self._loader: PluginLoader | None = None
        self._plugins: list[AgoraPlugin] = []
        self._mcp: FastMCP[None] | None = None

    @property
    def config(self) -> dict[str, object]:
        """Return the server configuration dict."""
        return self._config

    @property
    def skip_transport(self) -> bool:
        """Return whether transport is skipped."""
        return self._skip_transport

    @property
    def database(self) -> Database | None:
        """Return the Database instance (None before start)."""
        return self._database

    @property
    def eventbus(self) -> EventBus | None:
        """Return the EventBus instance (None before start)."""
        return self._eventbus

    @property
    def plugins(self) -> list[AgoraPlugin]:
        """Return the list of loaded plugins."""
        return list(self._plugins)

    async def start(self) -> None:
        """Start the server: create components, load plugins, register tools.

        Steps:

            1. Create Database and connect.
            2. Create EventBus.
            3. Create AgentRegistry and run migrations.
            4. Create RequestRouter.
            5. Create PluginLoader and load plugins.
            6. Register backbone tools on router.
            7. Register plugin tools on router.
            8. Create FastMCP server with AuthMiddleware.
            9. Register all router tools with FastMCP.
            10. Start stdio transport (unless skip_transport).
        """
        db_path: str = str(self._config.get("db_path", _DEFAULT_DB_PATH))
        plugins_cfg = cast(
            "list[dict[str, object]]", self._config.get("plugins", []),
        )

        # 1. Database
        self._database = Database(db_path)
        await self._database.connect()

        # 2. EventBus
        self._eventbus = EventBus()

        # 3. AgentRegistry
        self._registry = AgentRegistry(
            database=self._database, eventbus=self._eventbus,
        )
        await self._registry.initialize()

        # 4. RequestRouter
        self._router = RequestRouter(
            registry=self._registry, eventbus=self._eventbus,
        )

        # 5. PluginLoader
        self._loader = PluginLoader(
            database=self._database, eventbus=self._eventbus,
        )
        plugins, plugin_tools = await self._loader.load_plugins(plugins_cfg)
        self._plugins = plugins

        # 6. Register backbone tools on router
        self._register_backbone_tools()

        # 7. Register plugin tools on router
        for tool_name, tool_def in plugin_tools.items():
            self._router.register_tool(tool_name, tool_def.handler)

        # 8. Create FastMCP server with AuthMiddleware
        self._mcp = FastMCP[None]("agora")
        self._mcp.add_middleware(AuthMiddleware(self._router))

        # 9. Register all router tools with FastMCP
        self._register_tools_with_mcp()

        # 10. Start transport
        if not self._skip_transport:
            await self._mcp.run_stdio_async()

    async def stop(self) -> None:
        """Stop the server: shutdown plugins in reverse, close database."""
        for plugin in reversed(self._plugins):
            await plugin.on_shutdown()

        if self._database is not None:
            await self._database.close()
            self._database = None

    async def call_tool(
        self, tool_name: str, args: dict[str, object],
    ) -> dict[str, object]:
        """Call a tool directly, bypassing FastMCP transport and auth.

        Invokes the handler registered on the router without going through
        the router's authentication or audit pipeline.  Intended for testing.

        Args:
            tool_name: Fully qualified tool name (e.g. ``"register"``).

            args: Keyword arguments to forward to the handler.

        Returns:
            The handler's result dict.

        Raises:
            KeyError: If no tool with *tool_name* is registered.

        """
        assert self._router is not None, "Server not started"
        if tool_name not in self._router.list_tools():
            msg = f"TOOL_NOT_FOUND: {tool_name}"
            raise KeyError(msg)
        handler = self._router._tools[tool_name]  # noqa: SLF001
        return await handler(**args)

    def _register_backbone_tools(self) -> None:
        """Register backbone management tools on the router."""
        assert self._router is not None

        self._router.register_tool("register", self._handle_register)
        self._router.register_tool("heartbeat", self._handle_heartbeat)
        self._router.register_tool("list_agents", self._handle_list_agents)
        self._router.register_tool("get_agent", self._handle_get_agent)
        self._router.register_tool(
            "get_agent_by_name", self._handle_get_agent_by_name,
        )

    def _register_tools_with_mcp(self) -> None:
        """Register all router tools as FastMCP tools.

        Creates explicit-parameter wrappers for each tool since FastMCP
        rejects functions with ``**kwargs``.  Each wrapper delegates to
        ``router.route()`` for dispatch and audit.
        """
        assert self._mcp is not None
        assert self._router is not None

        for tool_name in self._router.list_tools():
            wrapper = _make_mcp_wrapper(self._router, tool_name)
            mcp_tool = Tool.from_function(wrapper, name=tool_name)
            self._mcp.add_tool(mcp_tool)

    # ── Backbone tool handlers ───────────────────────────────────

    async def _handle_register(
        self, *_args: object, **kwargs: object,
    ) -> dict[str, object]:
        """Register an agent with the backbone.

        Accepts keyword arguments: ``name``, ``role``, ``capabilities``,
        ``manifest``.

        Returns:
            Dict containing the ``agent_id``.

        """
        assert self._registry is not None
        name_val = str(kwargs.get("name", ""))
        role_val: str | None = str(kwargs["role"]) if "role" in kwargs else None
        caps_val: list[str] | None = None
        if "capabilities" in kwargs:
            raw_caps = kwargs["capabilities"]
            if isinstance(raw_caps, list):
                caps_val = [str(c) for c in raw_caps]
        manifest_val: dict[str, object] | None = None
        if "manifest" in kwargs:
            raw_manifest = kwargs["manifest"]
            if isinstance(raw_manifest, dict):
                manifest_val = dict(raw_manifest)
        agent_id = await self._registry.register(
            name=name_val,
            role=role_val,
            capabilities=caps_val,
            manifest=manifest_val,
        )
        return {"agent_id": agent_id}

    async def _handle_heartbeat(
        self, *_args: object, **kwargs: object,
    ) -> dict[str, object]:
        """Update the last heartbeat timestamp for an agent.

        Accepts keyword argument: ``agent_id``.

        Returns:
            Dict containing ``ok: True``.

        """
        assert self._registry is not None
        await self._registry.heartbeat(str(kwargs.get("agent_id", "")))
        return {"ok": True}

    async def _handle_list_agents(
        self, *_args: object, **_kwargs: object,
    ) -> dict[str, object]:
        """List all registered agents.

        Returns:
            Dict containing ``agents`` list.

        """
        assert self._registry is not None
        agents = await self._registry.list_agents()
        return {"agents": agents}

    async def _handle_get_agent(
        self, *_args: object, **kwargs: object,
    ) -> dict[str, object]:
        """Retrieve an agent by its UUID.

        Accepts keyword argument: ``agent_id``.

        Returns:
            Dict containing ``agent`` dict or None.

        """
        assert self._registry is not None
        agent = await self._registry.get_agent(str(kwargs.get("agent_id", "")))
        return {"agent": agent}

    async def _handle_get_agent_by_name(
        self, *_args: object, **kwargs: object,
    ) -> dict[str, object]:
        """Retrieve an agent by its unique name.

        Accepts keyword argument: ``name``.

        Returns:
            Dict containing ``agent`` dict or None.

        """
        assert self._registry is not None
        agent = await self._registry.get_agent_by_name(str(kwargs.get("name", "")))
        return {"agent": agent}
