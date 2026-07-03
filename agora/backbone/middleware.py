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

        Extracts the session_id from the FastMCP context and the tool
        name from the request parameters.  If the caller is not
        registered and the tool is not ``register``, raises ToolError.

        Args:
            context: The middleware context containing the tool call params.

            call_next: Callable that invokes the next handler in the chain.

        Returns:
            The result from the next handler if authorized.

        Raises:
            ToolError: If the caller is not authenticated and the tool
                is not ``register``.

        """
        # Extract session_id from the FastMCP context.
        fastmcp_ctx = context.fastmcp_context
        session_id: str | None = None
        if fastmcp_ctx is not None:
            session_id = getattr(fastmcp_ctx, "session_id", None)

        # Extract tool name from request params.
        tool_name: str | None = getattr(context.message, "name", None)

        # Authenticate via the RequestRouter.
        agent_id = await self._router.authenticate(session_id)

        # Reject if not authorized (unless it's the register tool).
        if agent_id is None and tool_name not in (None, _REGISTER_TOOL):
            msg = "NOT_AUTHORIZED"
            raise ToolError(msg)

        return await call_next(context)
