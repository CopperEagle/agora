# Agent Manifest Conventions

**Type:** REFERENCE | **Dependencies:** 001-backbone-scaffold (registry is built into the backbone)

## What

This is a conventions document, not a build task. The agent registry is part of the backbone (see task 001). This file defines the **standardized vocabulary and manifest conventions** that agents should follow when registering, so they're discoverable and interoperable.

## Capability Vocabulary

Agents declare capabilities as an array of standardized strings. Use these conventions:

| Category | Capability | Description |
|----------|-----------|-------------|
| Code | `code:write` | Writes new code |
| Code | `code:review` | Reviews code for bugs, style, security |
| Code | `code:refactor` | Restructures existing code |
| Code | `code:test` | Writes and runs tests |
| Code | `code:debug` | Debugs failures |
| Web | `web:search` | Searches the web for information |
| Web | `web:fetch` | Fetches and processes web pages |
| Web | `web:monitor` | Watches web pages for changes |
| Content | `text:write` | Writes prose (blog, docs, reports) |
| Content | `text:review` | Reviews prose for quality, tone, clarity |
| Content | `text:edit` | Edits existing text |
| Research | `research:paper` | Reads and analyzes research papers |
| Research | `research:topic` | Deep research on a topic |
| Research | `research:competitive` | Competitive intelligence |
| Meta | `meta:observe` | Scans environment for changes/opportunities |
| Meta | `meta:plan` | Decomposes goals into task plans |
| Meta | `meta:integrate` | Resolves conflicts and merges work |
| Meta | `meta:document` | Generates and maintains documentation |
| Meta | `meta:improve` | Analyzes performance, proposes improvements |
| Tool | `tool:build` | Creates new MCP tools |
| Tool | `tool:generate` | Generates MCP servers from repo surfaces |

## Manifest Standard Fields

When an agent registers, the manifest JSON should follow these conventions:

```jsonc
{
  "model": "claude-sonnet-4-20250514",   // Required — which model powers this agent
  "context_limit": 200000,                // Required — context window size
  "token_budget": 50000,                  // Optional — max tokens per session
  "preferred_temperature": 0.3,           // Optional — model temperature preference
  "author": "opencode",                   // Optional — who created this agent
  "version": "1.2.0",                     // Optional — agent definition version
  "description": "Reviews code for bugs, style issues, and security vulnerabilities",
  "home_channel": "#review",              // Optional — default Agora channel
  "triggers": ["review:code", "review:prose"]  // Optional — signals this agent responds to
}
```

Agents may add custom fields. The registry stores the manifest as opaque JSON.

## Registration Flow (Reference)

The backbone handles the mechanics (task 001). Agents should follow this sequence:

1. **Connect** → MCP session starts
2. **Register**: `register(name="reviewer", role="reviewer", capabilities=["code:review", "text:review"], manifest={...})`
3. **Heartbeat**: `heartbeat()` every N minutes (the backbone enforces this)
4. **Status updates**: `set_status("busy", task="reviewing PR #42")` as work progresses
5. **Discovery**: Other agents call `find_agents("code:review")` to find a reviewer
6. **Disconnect** → session ends, status = "offline" after 3 missed heartbeats

## Why This Matters

Without standardized conventions, agents declare themselves differently and discovery fails. The capability vocabulary ensures that a Planner asking for `code:review` finds the Reviewer agent, not a mis-tagged tool builder.

These conventions are not enforced by the backbone — they're conventions for agent authors. But agents that follow them are discoverable; agents that don't are invisible to discovery.

## Relevant Context

- **Task 001** — the backbone registry implementation (this file is the convention companion)
- **agent-archetypes/META.md** — each agent's role and capabilities should follow this vocabulary
- The capability vocabulary should be extended as new agent types are created
