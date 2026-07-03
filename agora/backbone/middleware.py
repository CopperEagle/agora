"""FastMCP middleware bridging RequestRouter auth with the MCP transport layer.

Intercepts ``tools/call`` requests before they reach plugin handlers.
Authenticated callers are forwarded; unauthenticated callers are
rejected with ``ToolError`` — except for the ``register`` tool, which
is always allowed.

Example::

    from agora.backbone.middleware import AuthMiddleware

    server = FastMCP("agora")
    server.add_middleware(AuthMiddleware(router))
"""

from __future__ import annotations

import json
import logging

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from agora.backbone.router import RequestRouter

logger = logging.getLogger(__name__)

_REGISTER_TOOL = "register"


class AuthMiddleware(Middleware):
    """FastMCP middleware that authenticates tool calls via RequestRouter.

    The ``register`` tool is always allowed (unauthenticated).
    All other tools require a registered session.

    Attributes:
        _router: RequestRouter used for authentication lookups.

    """

    def __init__(self, router: RequestRouter) -> None:
        """Initialize the middleware with a RequestRouter.

        Args:
            router: RequestRouter used for authentication lookups.

        """
        self._router = router

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Intercept tool calls and enforce authentication.

        Extracts ``_agent_id`` from the tool call arguments and
        validates it against the registry.  This is transport-agnostic
        — it works identically over stdio, SSE, and Streamable HTTP.

        The ``register`` tool is always allowed without authentication.
        All other tools require a valid ``_agent_id``.

        Args:
            context: The middleware context containing the tool call params.

            call_next: Callable that invokes the next handler in the chain.

        Returns:
            The result from the next handler if authorized.

        Raises:
            ToolError: If the caller is not authenticated and the tool
                is not ``register``.

        """
        # Extract tool name from request params.
        tool_name: str | None = getattr(context.message, "name", None)

        # Extract _agent_id from tool call arguments (transport-agnostic).
        # This works identically across stdio, SSE, and Streamable HTTP
        # transports — no session_id dependency.
        args = context.message.arguments or {}
        raw_agent_id: str | None = args.get("_agent_id", None)
        agent_id = str(raw_agent_id) if raw_agent_id is not None else None

        # Validate agent_id exists in registry.
        valid_agent_id: str | None = None
        if agent_id is not None:
            valid_agent_id = await self._router.authenticate(agent_id)

        # Reject if not authorized (unless it's the register tool).
        if valid_agent_id is None and tool_name not in (None, _REGISTER_TOOL):
            available_tools = self._router.list_tools()
            tool_list = ", ".join(available_tools[:15])
            error_text = json.dumps(
                {
                    "error": "NOT_AUTHORIZED",
                    "message": f"Missing or invalid _agent_id for tool '{tool_name}'.",
                    "details": {"tool": tool_name, "available_tools": tool_list},
                    "fix": (
                        "Call register({name: '...'}) first to obtain an agent_id, "
                        "then include it as _agent_id in subsequent calls."
                    ),
                },
            )
            raise ToolError(error_text)

        # Inject the authenticated agent_id into tool call arguments so
        # the handler receives it as _agent_id.
        if valid_agent_id is not None and tool_name not in (None, _REGISTER_TOOL):
            # Remove original _agent_id, add validated one.
            args.pop("_agent_id", None)
            args["_agent_id"] = valid_agent_id

        return await call_next(context)
