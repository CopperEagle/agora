# The Agora — Shared Agent Environment

**Status:** NOW — Foundation | **Priority: 1** (build before anything else)

## Name Origin

The name **Agora** (from ancient Greek ἀγορά *agorá*) was chosen as a metaphor — not drawn from any specific research paper. It evokes the central square of a Greek *polis*: the place where citizens gather to debate, trade, make decisions, share news, and hold each other accountable. Socrates held philosophy in the Agora. Democracy was practiced there. It was not a temple (hierarchy) or a palace (command) but a **shared environment** that the community co-inhabited.

The concept this name labels — a shared persistent environment for agent coordination — is directly inspired by blackboard architecture research: **PatchBoard** (arXiv:2605.29313), **LbMAS** (arXiv:2507.01701), and **BIGMAS** (arXiv:2603.15371). But the name itself is original to this project.

## Vision

The Agora is a shared MCP server that provides the infrastructure for multi-agent collaboration. Not a hierarchy. Not a passive tool. An **environment that agents co-inhabit**.

The core insight: agents should coordinate through shared persistent state, not through direct peer-to-peer messaging. Each agent reads what others have written and leaves traces of its own work. Coordination is **environment-mediated**, not orchestrator-mediated.

## Why MCP (not filesystem)

The simpler alternative (shared filesystem + flock) was considered and rejected:

| Concern | Filesystem | MCP Server |
|---------|-----------|------------|
| Concurrent writes | Race conditions, interleaved content | SQLite WAL transactions — atomic |
| Schema enforcement | None — any agent can write malformed data | Zod/JSON Schema validation on every write |
| Access control | None — any agent can rm -rf entire state | Per-tool, per-agent permissions |
| Observability | No built-in logging | Every call logged with agent_id, timestamp, outcome |
| Crash recovery | Partial writes, corrupted state | SQLite ACID guarantees |
| Extensibility | New convention per feature | New tool = new handler in the same server |
| One rogue agent | Can destroy everything | Can at worst spam (rate-limited) |

**Single-process, SQLite-backed, ~400-600 lines.** Not a distributed system. A local server that gives agents a clean, safe API for coordination.

## Architecture

```
 ┌─────────────────────────────────────────────────────────────┐
 │                    BACKBONE (core server)                     │
 │  Transport (stdio/HTTP) → Agent Registry → Request Router    │
 │  Event bus → Lifecycle Manager → Config Store                │
 │  Tables: backbone_agents, backbone_sessions, backbone_config │
 └───────────────────────────┬─────────────────────────────────┘
                             │
 ┌───────────────────────────▼─────────────────────────────────┐
 │                       PLUGIN API                             │
 │  AgoraPlugin: on_load, on_startup, on_shutdown, get_tools,  │
 │  get_migrations, on_agent_register, on_agent_disconnect     │
 └──┬───────────┬─────────────┬──────────────┬─────────────────┘
    │           │             │              │
 ┌──▼────┐ ┌───▼────┐ ┌─────▼────┐ ┌──────▼──────────┐
 │ CHAT  │ │ BOARD  │ │  LOG     │ │ MEMORY          │
 │Plugin │ │ Plugin │ │  Plugin  │ │ Plugin (next)   │
 │       │ │        │ │          │ │                 │
 │ chat_ │ │ board_ │ │  log_    │ │ mem_store       │
 │ post  │ │ write  │ │  query   │ │ mem_search      │
 │ read  │ │ read   │ │  costs   │ │ mem_recall      │
 └───────┘ └────────┘ └──────────┘ └─────────────────┘

  All plugins share: SQLite (WAL) | Event bus | Lock/Signal utilities
  Agent identity: Authenticated by backbone before reaching any plugin
```

## Research Backing

- **PatchBoard** (arXiv:2605.29313): Replacing inter-agent dialogue with validated JSON Patch mutations over shared structured state achieved 84.6% success vs 30.8% (LangGraph), using 45.5K vs 368.3K tokens. Provides replayable audit logs.
- **LbMAS** (arXiv:2507.01701): Blackboard architecture for LLM MAS. Shared blackboard with public/private spaces, LLM-based Control Unit. SOTA with fewer tokens than static or autonomous MAS.
- **BIGMAS** (arXiv:2603.15371): "Agents don't talk to each other — they all write to and read from a single shared workspace."
- **CAID** (arXiv:2603.21489): Git worktree isolation + automated merge = +26.7% on PaperBench. Validates that isolation + structured merge is the right execution pattern.
- **10+ existing MCP coordination servers** (ChatNut, collab-mcp, agent-room, agent-coord-mcp, group-chat-mcp, junto-memory, AOL, MACP) — validates the pattern exists; none are production-grade enough to adopt directly.

## Dependencies

| Depends on | For what |
|-----------|---------|
| opencode with MCP support | Already exists. MCP servers configurable in opencode.json. |
| A local SQLite installation | Pre-installed on all major OS. |
| Nothing else | This is the foundation — no other projects need to exist first. |

## Used By

| Project | How they use The Agora |
|---------|----------------------|
| agent-archetypes | All agents coordinate through Agora chat + board |
| knowledge-work | Tools post results to Agora; agents coordinate in channels |
| repo-to-mcp | Generated servers inject Agora meta-tools for cross-server messaging |
| meta-improvement | Reads activity log, publishes hypotheses, tracks experiments |

## Open Questions

1. Should the server use Streamable HTTP or stdio transport? HTTP enables remote agents; stdio is simpler for local use.
2. Should `summarize_channel` be server-side (the server calls an LLM) or client-side (the agent calls an LLM on fetched messages)? Server-side is cleaner but couples the server to a model.
3. Should there be a "watch" mechanism (SSE push) or is polling sufficient? Cloud models with 200K+ context make polling cheap, but SSE is more elegant for real-time.
4. How do we handle an agent that crashes mid-write to a board topic? SQLite handles this, but what about logical inconsistency (wrote A but not B)?
