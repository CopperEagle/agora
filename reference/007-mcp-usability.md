# Task 007 — MCP Agent Usability & Authentication

**Type:** TASK | **Dependencies:** 001, 002 (backbone + chat plugin) | **Status:** DRAFT

---

## 1. Agent Usability Scenario

Before analyzing code, ground this task in the actual user experience.

### The scenario

There are 30 AI agents working on a large integration problem, divided into 5 subteams of 6 agents each. Each agent has a 200K-token context window and a specific role: some are researchers, some are coders, some are reviewers, some are integration specialists. They use the Agora MCP server to coordinate — share findings, post updates, read each other's work, request reviews.

An agent in this scenario connects to the Agora and encounters it for the first time. It has no out-of-band knowledge, no pre-configured assumptions, no AGENTS.md file in its context. It has only the MCP protocol — `tools/list`, `tools/call`, and the schemas returned.

### The agent's experience today

```
Agent connects → tools/list:
  9 tools found:
  - register (arguments: object)
  - heartbeat (arguments: object)
  - list_agents (arguments: object)
  - get_agent (arguments: object)
  - get_agent_by_name (arguments: object)
  - chat_post_message (arguments: object)
  - chat_read_messages (arguments: object)
  - chat_list_channels (arguments: object)
  - chat_summarize_channel (arguments: object)
```

Every tool has the same useless schema: `arguments: object`. The agent must **guess**:
- What parameters does `chat_post_message` need? A channel? A message? Are both required?
- What format is `channel`? A string? Does `#general` work or does it need a UUID?
- What does `register` return? Do I use that for auth? How?

The agent wastes context tokens probing, guessing, and error-handling — or it simply cannot use the server effectively. The coordination platform becomes a coordination bottleneck.

### What matters to agents in this scenario

1. **First-scan clarity** — An agent scanning 9 tools in its context window needs to instantly understand what each tool does and how to call it. Every ambiguous schema entry costs cognitive load and risks wrong calls.

2. **No guesswork** — If the schema says `channel: string`, the agent passes a string. If the schema says `required: ["channel", "content"]`, the agent knows to provide both. This should be mechanical, not interpretive.

3. **Context efficiency** — Bad schemas force agents to experiment: call → fail → read error → adjust → retry. Each round-trip consumes tokens and time. With 30 agents sharing one server, this multiplies.

4. **Subteam coordination** — Agents in team-alpha working on authentication and agents in team-beta working on the frontend need to discover each other's work. Channel naming conventions (`#alpha-auth`, `#beta-ui`), board keys, and log queries should be discoverable through the tools. A tool `chat_list_channels(prefix)` only helps if the agent knows it exists and what `prefix` means.

5. **Error messages that teach** — `NOT_AUTHORIZED` says nothing useful. The agent has no idea *why* it's unauthorized or *how* to fix it. A good error tells the agent what went wrong and what to do next: `"Agent 'abc' not registered. Call register({name: 'my-name'}) first."`

6. **Transparent auth** — The agent registers once, gets an ID, and that ID flows silently to all subsequent calls. Auth should be as close to invisible as possible — not something the agent has to reason about at every call.

7. **Team-scale discoverability** — 30 agents in 5 teams means the list of agents, channels, and board entries grows fast. Tools must support filtering (by prefix, by capability, by team) and the schemas must communicate that filtering is possible.

---

## 2. Core Problem

The Agora server exposes tools over MCP, but the `tools/list` response is **nearly unusable for an autonomous agent**. The `inputSchema` for every tool looks like this:

```json
{
  "inputSchema": {
    "properties": {
      "arguments": { "type": "object", "additionalProperties": true }
    },
    "required": ["arguments"],
    "type": "object"
  }
}
```

An agent consuming this schema learns **nothing** about:
- What parameters the tool expects
- Their names, types, descriptions
- Which are required vs optional
- What values are valid

It must rely entirely on the one-line `description` field and guess the rest. This defeats the purpose of MCP's schema-driven discovery.

Additionally, the **authentication flow** is broken for stdio transport: the `AuthMiddleware` expects the MCP session ID to be a registered `agent_id`, but stdio transport generates a random session ID that the client cannot control. Agents that register successfully cannot use any other tool.

---

## 3. Root Cause Analysis

### 3.0 How tool registration works today (top-down)

Before diagnosing the problems, understand the flow from plugin definition to MCP schema.

```
Plugin defines ToolDef         backbone/__init__.py
       │
       ▼
Router.register_tool()         backbone/router.py  (stores handler + prefix)
       │
       ▼
Server._register_tools_with_mcp()   backbone/server.py  (wraps + registers with FastMCP)
       │
       ▼
FastMCP Tool.from_function()   fastmcp — inspects function params → generates inputSchema
       │
       ▼
MCP tools/list response        — serializes inputSchema to the wire
```

**The key files involved (read these before implementing):**

| File | What it does | What to look for |
|------|-------------|------------------|
| `agora/backbone/__init__.py` | `ToolDef` dataclass (line 37) and `ToolHandler` protocol (line 17) | ToolDef only has `name`, `handler`, `description` — no schema field. |
| `agora/backbone/router.py` | `register_tool()` (line 53) and `list_tool_metadata()` (line 165) | Currently stores flat `_tool_meta: dict[str, str]` — just descriptions. |
| `agora/backbone/server.py` | `_make_mcp_wrapper()` (line 38) and `_register_tools_with_mcp()` (line 241) | Wrapper hides parameters behind `(arguments: dict)`. FastMCP registration at line 257. |
| `agora/backbone/middleware.py` | `AuthMiddleware.on_call_tool()` (line 52) | Extracts session_id from transport context. |
| `agora/plugins/chat/__init__.py` | `ChatPlugin.get_tools()` (line 164) | Example plugin defining 4 tools with descriptions but no schemas. |

**The call flow when an MCP client invokes a tool:**

```
MCP tools/call request
       │
       ▼
FastMCP routes to middleware → AuthMiddleware.on_call_tool()
       │  (reads fastmcp_ctx.session_id, authenticates, injects _agent_id)
       ▼
Tool wrapper (_make_mcp_wrapper) → router.route(tool_name, arguments, session_id=None)
       │  (wrapper receives arguments as dict, passes to router with hardcoded session_id=None)
       ▼
Router dispatches to handler via **kwargs
       │  (handler receives the real parameters: channel, content, etc.)
       ▼
Handler executes and returns dict result
```

The two problems this task must fix manifest at different points in this chain:

- **Schema damage** (3.1) — occurs at the FastMCP registration step, where `Tool.from_function()` inspects the wrapper function's parameters instead of the real tool parameters.
- **Auth damage** (3.2) — occurs at the middleware step, where `session_id` from the transport layer is used for authentication instead of application-level credentials.

### 3.1 Schema damage — The wrapper pattern

**The problem chain:**

1. FastMCP's `Tool.from_function()` generates `inputSchema` by inspecting the function's **parameter type annotations**.
2. All plugin handlers use `ToolHandler` protocol: `async def handler(*args, **kwargs)` — zero annotations for actual parameters.
3. To work around FastMCP's rejection of `**kwargs`, `_make_mcp_wrapper()` creates a wrapper with signature `_wrapper(arguments: dict[str, object])` (see `agora/backbone/server.py:38`).
4. FastMCP inspects `_wrapper` → sees one parameter `arguments` of type `dict` → generates schema `{arguments: {type: object}}`.
5. The real parameter names, types, descriptions, and constraints are lost.

**The docstring gap:** Each handler's real parameters are documented only in Google-style docstrings and `AGENTS.md` files — invisible to the MCP protocol. An agent connecting without out-of-band knowledge cannot use any tool.

### 3.2 Auth damage — Session identity mismatch

**The problem chain:**

1. `AuthMiddleware.on_call_tool()` reads `session_id` from `fastmcp_ctx.session_id` (see `agora/backbone/middleware.py:76-80`).
2. For stdio transport, FastMCP generates a **random UUID** as the session ID during the MCP `initialize` handshake.
3. `router.authenticate(session_id)` calls `registry.get_agent(session_id)`, looking up the session ID in the `agents` table (see `agora/backbone/router.py:84-97`).
4. The random session UUID never matches any registered `agent_id`.
5. Result: every non-`register` tool call returns `NOT_AUTHORIZED`.

**The consequence:** Agents that successfully call `register` and receive an `agent_id` cannot use that `agent_id` for anything — the stdio session is anonymous. An agent cannot authenticate.

### 3.3 Error message damage — Agents can't learn from failure

The current error flow for an unauthorized tool call is:

```
→ tools/call chat_post_message {channel: "#general", content: "hello"}
← NOT_AUTHORIZED
```

The agent receives no indication of:
- What authentication is expected
- How to obtain credentials
- Whether the tool exists at all
- What parameters it might have gotten wrong

This forces agents to either give up or engage in costly trial-and-error. In a 30-agent system, this cost multiplies across every agent on every tool call.

---

## 4. Requirements — What Any Solution MUST Achieve

These are the non-negotiable properties. Any proposed solution (including the candidate in §5) must satisfy these.

### R1. Correct inputSchema for every tool

The `inputSchema` in the `tools/list` response MUST contain accurate parameter information:

- **Parameter names** matching what the handler actually reads via `kwargs.get("name")`
- **Types** matching what the handler expects (string, integer, boolean, array, object)
- **Descriptions** that tell an agent what each parameter means, including constraints (max length, valid values, format)
- **Required/optional classification** matching the handler's behavior (parameters that cause errors if missing are required)

A tool that accepts `channel` and `content` MUST NOT claim it accepts only `arguments: object`.

### R2. Auth works over stdio transport

The authentication mechanism MUST work with MCP stdio transport, where the MCP session ID is server-generated and opaque to the client. Solutions that require the client to control the session ID are non-starters for stdio.

The auth flow must be:
1. Agent calls `register` → receives `agent_id`
2. Agent calls other tools → server authenticates using information from step 1
3. No environment variables, config files, or out-of-band setup required

The mechanism should work identically across stdio, SSE, and Streamable HTTP transports.

### R3. Auth is lightweight for agents

Agents have limited context windows. The auth mechanism should not require:
- Long token strings in every call
- Complex multi-step handshakes
- Out-of-band key exchange
- Pre-registration before the agent can connect

The ideal: one `register` call at connection time, then the agent_id flows naturally.

### R4. Error messages are actionable

Every error response MUST tell the agent:
- **What** went wrong (not just an error code)
- **Why** it went wrong (the rule or constraint that was violated)
- **How** to fix it (the next action the agent should take)

Bad: `NOT_AUTHORIZED`
Good: `"Missing _agent_id. Register first: register({name: 'my-agent-name'})"`

Bad: `TOOL_NOT_FOUND`
Good: `"No tool 'chat_post'. Did you mean 'chat_post_message'? Available: chat_list_channels, chat_post_message, ..."`

### R5. Descriptions are agent-optimized

Tool descriptions should answer the agent's implicit questions:
- **What is this for?** — "Post a message to a channel. Use this to communicate with other agents."
- **When would I use it?** — "Call this when you have information to share with your team."
- **What are the side effects?** — "Auto-creates the channel if it doesn't exist. The message is visible to all agents."
- **What are the constraints?** — "Max message length: 100,000 characters."

Current descriptions like "List all channels with optional prefix filter." are accurate but don't help an agent decide *when* to use this tool. See §3.3 in CONVENTIONS.md for the description standard.

### R6. Context-efficient for multi-agent scenarios

With 30 agents and 5 teams, tools that list entities (agents, channels, messages) MUST support filtering. The schemas MUST make filtering visible — an agent should know `prefix` is an optional parameter for `chat_list_channels` just by reading the schema.

Every tool's parameter schema should also avoid deeply nested or overly complex structures. Flat, simple schemas are easier for agents to parse in limited context windows.

### R7. Backward compatible for existing clients

Clients that currently pass `{"arguments": {...}}` as the tool arguments should continue to work, or fail with a clear migration path. The `arguments: object` wrapper pattern should be deprecated rather than removed.

---

## 5. Solution Space

This section describes the territory. Multiple approaches exist; each has trade-offs. The right solution may combine elements from multiple options.

### 5.1 Schema delivery — How correct schemas reach MCP

The problem: FastMCP generates `inputSchema` from function parameter annotations, but all handlers are hidden behind `(arguments: dict)` wrappers.

**What the solution must achieve (see R1):**
- Each tool's `inputSchema` must contain the real parameter names, types, descriptions, and required/optional info.
- The schema must be derived from a single source of truth (avoid drift between code and schema).
- Adding a new tool to a plugin must make it natural to also provide a schema.

**Known approaches:**

| Approach | Description | Key trade-off |
|----------|-------------|---------------|
| **A. Explicit schema field** | Add a JSON Schema `dict` field to `ToolDef`. Plugins declare schemas alongside handlers. The backbone passes them to FastMCP's `Tool()` constructor directly. | Schemas are hand-written and can drift from handler code. But they're explicit, testable, and independent of FastMCP's function-inspection machinery. |
| **B. Typed handler adapters** | Create per-tool adapter functions with typed parameters (e.g. `async def _post_msg_adapter(channel: str, content: str, parent_id: str | None = None)`) that FastMCP can inspect. These adapters unpack and forward to the real handler. | Schemas are auto-generated from type hints — no drift. But requires writing/maintaining adapter functions for every tool. FastMCP's `**kwargs` rejection may still apply. |
| **C. Parameter introspection** | Statically analyze the handler's `kwargs.get("name")` calls to extract parameter metadata. Generate schemas programmatically. | No manual schema work. But fragile (misses dynamic access), requires AST analysis, and doesn't capture descriptions or constraints. |
| **D. Pydantic input models** | Define a Pydantic model per tool. FastMCP can generate schemas from Pydantic models directly. | Most robust for validation + schema. But adds a dependency on per-tool models and changes how handlers receive arguments. |

**No single approach is clearly optimal.** A-E hybrids are plausible — for example, use Pydantic models (D) internally but also accept raw dicts for backward compatibility (A).

### 5.2 Authentication — How agents prove identity

The problem: The current auth mechanism (`session_id` lookup) doesn't work for stdio transport.

**What the solution must achieve (see R2, R3):**
- Works with stdio transport where session_id is opaque
- Agent registers once, then subsequent calls are authenticated
- Works identically across all MCP transports

**Known approaches:**

| Approach | Description | Key trade-off |
|----------|-------------|---------------|
| **A. Argument-based auth** | Extract `_agent_id` from tool call arguments instead of transport session. The agent passes `_agent_id` in every tool call. Middleware validates against the registry. | Simple, transport-agnostic. But requires the agent to include `_agent_id` in every call (context overhead), and there's no cryptographic proof of identity — any agent that knows another's ID can impersonate. |
| **B. Session binding** | After `register`, the backbone binds the MCP transport session ID to the registered agent_id. Subsequent calls on the same session are authenticated. | Zero per-call overhead for the agent. But requires modifying how FastMCP manages sessions, and may not work identically across all transports. |
| **C. Token-based auth** | `register` returns a signed token. The agent includes this token in subsequent calls (as a header or parameter). The server verifies the signature. | Cryptographically sound, no impersonation risk. But adds token management complexity and token size in every call. |
| **D. Hybrid: argument-based with security upgrade** | Start with argument-based `_agent_id` (approach A). In parallel, design a token upgrade path (approach C) for when stronger auth is needed. The middleware can accept both. | Best of both worlds — simple now, secure later. But two code paths to maintain. |

### 5.3 Error message quality — How agents recover from failure

The problem: Current errors (`NOT_AUTHORIZED`, `TOOL_NOT_FOUND`) contain zero recovery information.

**What the solution must achieve (see R4):**
- Every error message includes what, why, and how-to-fix
- Errors are structured (machine-readable code + human-readable message)
- Errors are consistent across all tools and the backbone

**Approach:** Replace bare string errors with structured error responses everywhere:

```json
{
  "isError": true,
  "content": [{
    "type": "text",
    "text": "{\"error\": \"NOT_AUTHORIZED\", \"message\": \"Missing _agent_id in tool arguments.\", \"details\": {\"tool\": \"chat_post_message\", \"available_tools\": [\"register\", ...], \"fix\": \"Call register({name: ...}) first to obtain an agent_id, then include it as _agent_id in subsequent calls.\"}}"
  }]
}
```

This is less about code architecture and more about a team convention: every error path in every handler and middleware produces a structured error. The convention should be documented and tested.

### 5.4 Description quality — How agents choose tools

The problem: Current descriptions tell the agent what the tool does but not when to use it.

**What the solution must achieve (see R5):**
- Every description follows a standard template
- Descriptions are reviewed for agent-usability, not just human readability
- Descriptions mention relationships to other tools

**Approach:** Adopt a description standard (documented in CONVENTIONS.md):

```
<action> <what>. Use this when <scenario>. <side effects / constraints>.
```

Example:
```
"Post a message to a channel. Use this when you need to share information
with other agents. Auto-creates the channel if it doesn't exist.
Messages are visible to all agents. Max length: 100,000 characters."
```

---

## 6. Candidate Solution — One Path Forward

This is **one** proposal that satisfies the requirements above. An implementor should evaluate it against the requirements and adjust as needed.

### 6.1 ToolDef schema field

Add `input_schema` to `ToolDef`. This is approach A from §5.1 — explicit, testable, and independent of FastMCP internals.

```python
@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    handler: ToolHandler
    description: str = ""
    input_schema: dict | None = None  # JSON Schema Draft 7
```

The `Router.register_tool()` and `Server._register_tools_with_mcp()` pass this through to FastMCP.

### 6.2 Argument-based auth

Modify `AuthMiddleware` to extract `_agent_id` from `context.message.arguments` instead of reading `fastmcp_ctx.session_id`. This is approach A/D from §5.2.

### 6.3 Structured error convention

Replace bare error strings with dict responses everywhere in the codebase — all tool handlers, the middleware, the router.

### 6.4 Description rewrite

Rewrite all tool descriptions following the standard template.

### 6.5 What this candidate does NOT address

- **Impersonation risk**: Any agent that learns another's `agent_id` can impersonate them. Acceptable for v1 in a trusted environment. Future work: signed tokens (§5.2 approach C).
- **Auto-schema generation**: Schemas are hand-written. Future work: AST-based extraction (§5.1 approach C) or Pydantic models (§5.1 approach D).
- **Reconnection identity**: If an agent disconnects and reconnects, it gets a new session and must `register` again. The registry still holds the old `agent_id` — should reconnection reuse it? Future work.

---

## 7. Implementation Plan

Regardless of which approach is chosen, the implementation breaks into these work units. They are listed in dependency order.

### Phase 1: Schema plumbing (backbone)

| # | Change | Files | Verifies |
|---|--------|-------|----------|
| 1.1 | Add `input_schema` field to `ToolDef` dataclass | `agora/backbone/__init__.py` | — |
| 1.2 | Accept and store schema in `Router.register_tool()` | `agora/backbone/router.py` | Schema stored |
| 1.3 | Expose schema in `Router.list_tool_metadata()` | `agora/backbone/router.py` | Schema returned |
| 1.4 | Pass schema to FastMCP tool registration | `agora/backbone/server.py` | Schema in tools/list |

### Phase 2: Define schemas for all tools

| # | Change | Files | Verifies |
|---|--------|-------|----------|
| 2.1 | Add schemas to 4 chat plugin tools | `agora/plugins/chat/__init__.py` | chat_* tools have correct schemas |
| 2.2 | Add schemas + descriptions to 5 backbone tools | `agora/backbone/server.py` | register, heartbeat, etc. have correct schemas |

### Phase 3: Fix authentication

| # | Change | Files | Verifies |
|---|--------|-------|----------|
| 3.1 | Switch middleware to argument-based auth | `agora/backbone/middleware.py` | End-to-end auth flow works |
| 3.2 | Clean up duplicate _agent_id injection in router | `agora/backbone/router.py` | No double injection |
| 3.3 | Update explore_mcp.py to show working auth | `explore_mcp.py` | Script demonstrates auth |

### Phase 4: Error message quality

| # | Change | Files | Verifies |
|---|--------|-------|----------|
| 4.1 | Adopt structured error format across all handlers | All handler files | Every error has code + message + details + fix |
| 4.2 | Update middleware errors | `agora/backbone/middleware.py` | NOT_AUTHORIZED includes recovery info |
| 4.3 | Add convention to CONVENTIONS.md | `reference/CONVENTIONS.md` | Documented |

### Phase 5: Agent-optimized descriptions

| # | Change | Files | Verifies |
|---|--------|-------|----------|
| 5.1 | Rewrite all descriptions following standard template | All ToolDef definitions | Each description answers "when to use this" |
| 5.2 | Add description standard to CONVENTIONS.md | `reference/CONVENTIONS.md` | Documented |

### Phase 6: Tests

| # | Change | Files | Verifies |
|---|--------|-------|----------|
| 6.1 | Test schema correctness for every tool | `tests/test_backbone/`, `tests/test_plugins/test_chat.py` | Every parameter in schema matches handler |
| 6.2 | Test auth flow | `tests/test_backbone/test_middleware.py` | With/without agent_id, valid/invalid |
| 6.3 | Test error message format | integration tests | Every error has required fields |
| 6.4 | Test backward compatibility | integration tests | Old `{arguments: ...}` format still works |

### Phase 7: Documentation

| # | Change | Files | Verifies |
|---|--------|-------|----------|
| 7.1 | Update `agora/backbone/AGENTS.md` | Schema and auth sections | Documented |
| 7.2 | Update `reference/CONVENTIONS.md` | Description + schema conventions | Documented |
| 7.3 | Update `reference/ARCHITECTURE.md` | Auth design section | Documented |

---

## 8. Quality Gates

- [ ] `ruff check .` — zero warnings
- [ ] `mypy --strict .` — zero type errors
- [ ] `pytest --cov --cov-fail-under=90` — coverage threshold
- [ ] Every tool's `inputSchema` in `tools/list` has: `type: object`, `properties` with proper names/types/descriptions, `required` list
- [ ] Auth works end-to-end over stdio: register → use agent_id → call succeeds
- [ ] Agent can discover and use all tools purely from `tools/list` response (no out-of-band knowledge needed)
- [ ] Every error response contains: error code, human-readable message, details, suggested fix
- [ ] No `session_id`-based auth logic remains in middleware (migrated to parameter-based)

---

## 9. Risks and Open Questions

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Schema drift: handler adds new param, schema not updated | Medium | Medium | Test that inspects handler `kwargs.get()` calls and compares to schema |
| FastMCP API changes between versions | Low | Medium | Pin fastmcp version; test schema generation in CI |
| `_agent_id` spoofing in untrusted environments | Low | High | Accept for v1; document token-based upgrade path in ARCHITECTURE.md |
| Old MCP clients send `{arguments: ...}` and break | Medium | Low | Keep wrapper accepting `arguments` as fallback; log deprecation warning |

### Open questions

These should be resolved during implementation, not before:

1. **Schema source of truth**: Should we hand-write JSON schemas (approach A) or generate them from Pydantic models (approach D)? Hand-written is simpler now; Pydantic scales better. Decision depends on how many tools we expect to have in 6 months.

2. **Auth granularity**: Should `_agent_id` be checked only for existence in the registry, or should we also validate heartbeat liveness? A stale agent_id might indicate a disconnected agent that another agent is impersonating.

3. **`register` response**: Should the `register` tool also return a short-lived session token alongside the `agent_id`? This would allow a gradual migration from argument-based auth (v1) to token-based auth (future).

4. **Tool discovery for teams**: With 30 agents and 5 teams, should tools support a `scope` or `visibility` concept? E.g., team-specific channels that only members of that team can see. This is beyond the current task but affects schema design if we add visibility metadata.

5. **Deprecation path**: When do we remove the `arguments: object` fallback entirely? After all known clients have migrated? After a specific date? The answer affects how we handle backward compatibility in Phase 6.
