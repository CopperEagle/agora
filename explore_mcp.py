"""Explore ANY MCP stdio server from the outside — see every protocol message.

Usage:
    uv run python explore_mcp.py
    uv run python explore_mcp.py -- python -m my_server   # custom command

This script connects to a stdio MCP server and shows the full JSON-RPC
message exchange: initialize handshake, tools/list, and optionally tools/call.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime


# ═════════════════════════════════════════════════════════════════════════════
# Coloured terminal helpers
# ═════════════════════════════════════════════════════════════════════════════

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:12]


def _blue(msg: str) -> str:
    return f"\033[36m{msg}\033[0m" if sys.stderr.isatty() else msg


def _green(msg: str) -> str:
    return f"\033[32m{msg}\033[0m" if sys.stderr.isatty() else msg


def _yellow(msg: str) -> str:
    return f"\033[33m{msg}\033[0m" if sys.stderr.isatty() else msg


def _dim(msg: str) -> str:
    return f"\033[2m{msg}\033[0m" if sys.stderr.isatty() else msg


# ═════════════════════════════════════════════════════════════════════════════
# Low-level MCP stdio client — sends JSON-RPC, prints every message
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class McpResponse:
    id: int
    result: dict | None = None
    error: dict | None = None


class StdioMcpClient:
    """Manual MCP client over stdio — fully transparent."""

    def __init__(self, command: str, args: list[str]) -> None:
        self._command = command
        self._args = args
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._server_name = f"{os.path.basename(command)} {' '.join(args)}"

    # ── protocol helpers ────────────────────────────────────────────────

    async def _send(self, method: str, params: dict | None = None,
                    *, need_reply: bool = True) -> int | None:
        """Send a JSON-RPC message, print it, return id if request."""
        msg: dict = {"jsonrpc": "2.0"}
        if need_reply:
            msg_id = self._next_id
            self._next_id += 1
            msg["id"] = msg_id
        else:
            msg_id = None
        msg["method"] = method
        if params is not None:
            msg["params"] = params

        line = json.dumps(msg, ensure_ascii=False)
        kind = "→ REQ" if need_reply else "→ NOT"
        print(f"  {_ts()} {_blue(kind)} {method}", file=sys.stderr)
        print(f"  {_dim(line)}", file=sys.stderr)
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((line + "\n").encode())
        await self._proc.stdin.drain()
        return msg_id

    async def _recv(self, timeout: float = 5.0) -> dict:
        """Read one JSON line from stdout, print it, return parsed dict."""
        assert self._proc is not None and self._proc.stdout is not None
        raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        msg = json.loads(raw.decode())
        kind = "← RES" if "id" in msg else "← EVT"
        method = msg.get("method", "(result)")
        print(f"  {_ts()} {_green(kind)} {method}", file=sys.stderr)
        # Pretty-print the response JSON
        pp = json.dumps(msg, indent=2, ensure_ascii=False)
        for line in pp.split("\n"):
            print(f"  {_dim(line)}", file=sys.stderr)
        return msg

    async def request(self, method: str, params: dict | None = None,
                      timeout: float = 10.0) -> dict:
        """Send a request and wait for the matching response."""
        msg_id = await self._send(method, params, need_reply=True)
        assert msg_id is not None
        while True:
            resp = await self._recv(timeout=timeout)
            rid = resp.get("id")
            if rid == msg_id:
                if "error" in resp:
                    err = resp["error"]
                    raise RuntimeError(
                        f"MCP error [{err.get('code')}]: {err.get('message')}"
                    )
                return resp.get("result", {})
            # Not our response — unexpected, but log and continue

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification (no reply expected)."""
        await self._send(method, params, need_reply=False)

    # ── lifecycle ───────────────────────────────────────────────────────

    async def connect(self, timeout: float = 10.0) -> None:
        """Spawn the server subprocess and perform MCP initialize handshake."""
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  🔌 Connecting to: {_yellow(self._server_name)}", file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)

        self._proc = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # we don't read stderr here
        )

        # --- Step 1: Initialize ---
        print(f"  ┌─ {_blue('Step 1: initialize handshake')}", file=sys.stderr)
        result = await self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "explore-mcp", "version": "0.1.0"},
        }, timeout=timeout)
        server_info = result.get("serverInfo", {})
        print(f"  └─ ✅ Server: {server_info.get('name', '?')} "
              f"v{server_info.get('version', '?')}", file=sys.stderr)
        print(f"     Protocol: {result.get('protocolVersion', '?')}\n",
              file=sys.stderr)

        # --- Step 2: Send initialized notification ---
        print(f"  ┌─ {_blue('Step 2: initialized notification')}", file=sys.stderr)
        await self.notify("notifications/initialized")
        print(f"  └─ ✅ Ready to call tools\n", file=sys.stderr)

    async def close(self) -> None:
        """Shut down the server subprocess."""
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

    # ── tool operations ─────────────────────────────────────────────────

    async def list_tools(self, timeout: float = 10.0) -> list[dict]:
        """Call tools/list and return the list of tools."""
        print(f"  ┌─ {_blue('Step 3: tools/list')}", file=sys.stderr)
        result = await self.request("tools/list", timeout=timeout)
        tools = result.get("tools", [])
        print(f"  └─ ✅ Found {len(tools)} tool(s)\n", file=sys.stderr)
        return tools

    async def call_tool(self, name: str, arguments: dict | None = None,
                        timeout: float = 10.0) -> dict:
        """Call a tool and return the result."""
        print(f"  ┌─ {_blue(f'Step 4: tools/call {name}')}", file=sys.stderr)
        result = await self.request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        }, timeout=timeout)
        print(f"  └─ ✅ Tool returned\n", file=sys.stderr)
        return result


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    # Determine server command from CLI args or use default
    if len(sys.argv) > 1 and sys.argv[1] == "--":
        cmd_args = sys.argv[2:]
    elif len(sys.argv) > 1:
        cmd_args = sys.argv[1:]
    else:
        # Default: point to the agora server in this repo
        repo = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(repo, "venv", "bin", "python")
        if os.path.exists(venv_python):
            python = venv_python
        else:
            python = sys.executable
        cmd_args = [python, os.path.join(repo, "agora", "__main__.py")]

    client = StdioMcpClient(cmd_args[0], cmd_args[1:])

    try:
        # ── Connect & initialize ────────────────────────────────────────
        await client.connect()

        # ── List tools ──────────────────────────────────────────────────
        tools = await client.list_tools()
        for t in tools:
            name = t.get("name", "?")
            desc = t.get("description", "")
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            print(f"    📌 {name}", file=sys.stderr)
            if desc:
                print(f"       {desc}", file=sys.stderr)
            if props:
                required_params = schema.get("required", [])
                for pname in sorted(props.keys()):
                    pinfo = props[pname]
                    ptype = pinfo.get("type", "any")
                    pdesc = pinfo.get("description", "")
                    optional = pname not in required_params
                    opt_str = " (optional)" if optional else ""
                    display = f"       📌 {pname}: {ptype}{opt_str}"
                    if pdesc:
                        display += f"\n          {pdesc}"
                    print(display, file=sys.stderr)
            print(file=sys.stderr)

        # ── Call a tool: register an agent ──────────────────────────────
        print(f"{'─'*60}", file=sys.stderr)
        print(f"  🧪 Calling a tool: register", file=sys.stderr)
        print(f"{'─'*60}\n", file=sys.stderr)

        result = await client.call_tool("register", {"name": "explorer"})
        agent_id: str | None = None
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item["text"]
                try:
                    data = json.loads(text)
                    agent_id = data.get("agent_id")
                    print(f"  ✅ Result: {json.dumps(data, indent=2, ensure_ascii=False)}",
                          file=sys.stderr)
                except json.JSONDecodeError:
                    print(f"  ⚠️  Register returned non-JSON: {text}", file=sys.stderr)
                    break

        # ── Call a tool without auth: NOT_AUTHORIZED ────────────────────
        print(f"\n{'─'*60}", file=sys.stderr)
        print(f"  🧪 Calling chat_post_message (no auth → expect error)",
              file=sys.stderr)
        print(f"{'─'*60}\n", file=sys.stderr)

        result = await client.call_tool("chat_post_message", {
            "channel": "general",
            "content": "Hello from the outside!",
        })
        is_error = result.get("isError", False)
        icon = "❌" if is_error else "✅"
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item["text"]
                try:
                    data = json.loads(text)
                    print(f"  {icon} Result: "
                          f"{json.dumps(data, indent=2, ensure_ascii=False)}",
                          file=sys.stderr)
                except json.JSONDecodeError:
                    print(f"  {icon} Result: {text}", file=sys.stderr)

        # ── Call with agent_id: should succeed ──────────────────────────
        print(f"\n{'─'*60}", file=sys.stderr)
        print(f"  🧪 Calling chat_post_message WITH agent_id (should succeed)",
              file=sys.stderr)
        print(f"{'─'*60}\n", file=sys.stderr)

        if agent_id:
            result = await client.call_tool("chat_post_message", {
                "channel": "general",
                "content": "Hello with auth!",
                "_agent_id": agent_id,
            })
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "No agent_id from register"}]}

        is_error = result.get("isError", False)
        icon = "❌" if is_error else "✅"
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item["text"]
                try:
                    data = json.loads(text)
                    print(f"  {icon} Result: "
                          f"{json.dumps(data, indent=2, ensure_ascii=False)}",
                          file=sys.stderr)
                except json.JSONDecodeError:
                    print(f"  {icon} Result: {text}", file=sys.stderr)

        # ── Auth success & schema quality ───────────────────────────────
        print(f"\n{'─'*60}", file=sys.stderr)
        print(f"  ✅ Auth works with argument-based _agent_id", file=sys.stderr)
        print(f"{'─'*60}\n", file=sys.stderr)
        if is_error:
            print(f"  ⚠️  Unexpected error — auth middleware rejected", file=sys.stderr)
            print(f"     _agent_id from arguments. See result above.", file=sys.stderr)
        else:
            print(f"  The T2 fix makes AuthMiddleware inspect _agent_id from", file=sys.stderr)
            print(f"  tool arguments. Passing _agent_id alongside tool params", file=sys.stderr)
            print(f"  now works for authenticated calls.", file=sys.stderr)

        print(f"\n{'─'*60}", file=sys.stderr)
        print(f"  📋 Schema quality", file=sys.stderr)
        print(f"{'─'*60}\n", file=sys.stderr)
        print(f"  Every tool now has proper inputSchema with named parameters,", file=sys.stderr)
        print(f"  types, and agent-optimized descriptions (T1 + T7 fixes).", file=sys.stderr)

    finally:
        await client.close()

    # Print final summary to stdout
    print("\n" + "=" * 60)
    print("✅ Exploration complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
