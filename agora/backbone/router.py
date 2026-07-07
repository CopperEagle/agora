"""Request router — authenticate, route, dispatch, and audit tool calls.

The router sits between MCP transport and plugin tool handlers.  It
authenticates callers via the ``AgentRegistry``, dispatches to the
correct handler, and emits ``tool.executed`` audit events.

Example::

    router = RequestRouter(registry=reg, eventbus=bus)
    router.register_tool("echo", my_echo_handler, prefix="chat")
    result = await router.route("chat_echo", {"msg": "hi"}, session_id=agent_id)
"""

from __future__ import annotations

from agora.backbone import ToolHandler
from agora.backbone.eventbus import EventBus
from agora.backbone.registry import AgentRegistry

_REGISTER_TOOL = "register"


class RequestRouter:
    """Authenticate, route, dispatch, and audit tool calls.

    The router owns the tool registry (name → handler mapping), delegates
    authentication to ``AgentRegistry``, and emits audit events on the
    ``EventBus`` after every successful dispatch.

    Attributes:
        _registry: Agent registry for authentication look-ups.

        _eventbus: In-process event bus for audit events.

        _tools: Mapping of tool name → handler callable.

    """

    def __init__(self, registry: AgentRegistry, eventbus: EventBus) -> None:
        """Initialize the router with shared infrastructure.

        Args:
            registry: Agent registry for authentication look-ups.

            eventbus: In-process event bus for audit events.

        """
        self._registry = registry
        self._eventbus = eventbus
        self._tools: dict[str, ToolHandler] = {}
        self._tool_meta: dict[str, str] = {}

    def register_tool(
        self,
        name: str,
        handler: ToolHandler,
        prefix: str | None = None,
        description: str = "",
    ) -> None:
        """Register a tool with its handler function.

        If *prefix* is provided the final name becomes ``f"{prefix}_{name}"``.

        Args:
            name: Short tool name (e.g. ``"post_message"``).

            handler: Async callable implementing the tool logic.

            prefix: Optional namespace prefix (e.g. ``"chat"``).

            description: Human-readable tool description exposed via MCP.

        Raises:
            ValueError: If a tool with the same final name is already registered.

        """
        final_name = f"{prefix}_{name}" if prefix else name
        if final_name in self._tools:
            msg = f"Duplicate tool name: {final_name}"
            raise ValueError(msg)
        self._tools[final_name] = handler
        self._tool_meta[final_name] = description

    async def authenticate(self, session_id: str | None) -> str | None:
        """Look up the agent by session_id and return its agent_id.

        Args:
            session_id: The session / agent identifier provided by the caller.

        Returns:
            The agent_id string if the agent is registered, ``None`` otherwise.

        """
        if session_id is None:
            return None
        agent = await self._registry.get_agent(session_id)
        return str(agent["id"]) if agent is not None else None

    async def route(
        self,
        tool_name: str,
        args: dict[str, object],
        session_id: str | None,
    ) -> dict[str, object]:
        """Authenticate the caller, dispatch to the handler, and audit.

        This is the main entry point called by FastMCP middleware (T9).

        Args:
            tool_name: Fully qualified tool name (e.g. ``"chat_post_message"``).

            args: Keyword arguments to forward to the handler.

            session_id: Caller session identifier (may be ``None``).

        Returns:
            The handler's result dict, or a structured error dict with keys
            ``error``, ``message``, ``details``, and ``fix``.

        """
        agent_id = await self.authenticate(session_id)

        if agent_id is None and tool_name != _REGISTER_TOOL:
            return {
                "error": "NOT_AUTHORIZED",
                "message": f"Agent not registered for tool '{tool_name}'",
                "details": {"tool": tool_name},
                "fix": "Call register({name: '...'}) first to obtain an agent_id, "
                "then include it as _agent_id in subsequent calls.",
            }

        if tool_name not in self._tools:
            available = ", ".join(sorted(self._tools))
            return {
                "error": "TOOL_NOT_FOUND",
                "message": f"Unknown tool '{tool_name}'",
                "details": {"tool": tool_name},
                "fix": f"Check tool name. Available: {available}",
            }

        handler = self._tools[tool_name]

        result = await handler(**args)

        # Update heartbeat AFTER handler completes (reflects actual activity,
        # not call start).  register is unauthenticated and does not reach
        # this point with an agent_id, so the guard is safe.
        if agent_id is not None:
            await self._registry.heartbeat(agent_id)

        await self._eventbus.emit(
            "tool.executed",
            tool=tool_name,
            agent_id=agent_id,
            result_keys=list(result.keys()),
        )

        return result

    def list_tools(self) -> list[str]:
        """Return sorted list of all registered tool names.

        Returns:
            Alphabetically sorted list of tool name strings.

        """
        return sorted(self._tools)

    def list_tool_metadata(self) -> list[dict[str, str]]:
        """Return sorted list of tool name and description dicts.

        Returns:
            List of dicts, each with ``"name"`` and ``"description"`` keys,
            sorted alphabetically by tool name.

        """
        return [
            {"name": name, "description": self._tool_meta.get(name, "")}
            for name in sorted(self._tools)
        ]
