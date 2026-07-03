# 006 — Memory Plugin

**Priority:** P1 | **Phase:** NEXT (research required before building) | **Dependencies:** 001-backbone-scaffold

## What

The **Memory plugin** provides persistent long-term memory for agents — facts, preferences, learned patterns, and past experiences that survive across sessions. Unlike Chat (ephemeral conversation), Board (current coordination state), or Log (audit trail), Memory is the system's persistent knowledge base.

Tool prefix: `mem_`

## Plugin Interface (Tentative)

```python
class MemoryPlugin(AgoraPlugin):
    name = "memory"
    version = "1.0.0"
    description = "Persistent long-term memory with semantic retrieval"

    def get_tools(self):
        return [
            ToolDef("mem_store", self.store, schema),       # Store a memory
            ToolDef("mem_recall", self.recall, schema),     # Retrieve by key
            ToolDef("mem_search", self.search, schema),     # Semantic/pattern search
            ToolDef("mem_forget", self.forget, schema),     # Remove a memory
            ToolDef("mem_stats", self.stats, schema),        # Memory usage stats
        ]

    # get_migrations() — table schema depends on design decisions below
```

## Tools (Design Sketch)

1. `mem_store(namespace, key?, content, metadata?, ttl?)` → memory_id
   - Store a memory in a namespace (agent-specific, shared, or project-scoped)
   - Key is optional — if omitted, auto-generated (for episodic memories)
   - Content is the memory payload (text, JSON, or structured data)
   - Metadata: tags, importance score, source agent, session, etc.
   - TTL: optional expiration (memories can auto-forget)

2. `mem_recall(namespace, key)` → memory | null
   - Retrieve a specific memory by key
   - Returns full memory record including metadata, creation time, source agent

3. `mem_search(namespace, query, limit=10, threshold?)` → memories[]
   - Search memories by semantic similarity or keyword match
   - Returns ranked results with relevance scores
   - Threshold: minimum relevance score (0.0-1.0) to include in results

4. `mem_forget(namespace, memory_id)` → success/failure
   - Remove a specific memory
   - Soft-delete (mark as forgotten) or hard-delete (remove from storage)?

5. `mem_stats(namespace?)` → usage statistics
   - Total memories, storage size, namespace breakdown, most active agents

## Why This Matters

Without memory, agents are amnesiac. They can't learn from past experiences, remember user preferences, or maintain consistency across sessions. The Memory plugin closes this gap.

Concrete use cases:
- An agent remembers "the user prefers verbose explanations for security topics but short answers for syntax questions"
- An agent recalls "we tried approach X for problem Y last week and it failed because of Z"
- An agent stores "the coding style convention for this project is K&R braces, 4-space indentation"
- An agent remembers "agent Reviewer is the fastest at reviewing Python code"

## HONEST GAPS — Research Required

The Memory plugin has several unresolved design questions. A strategizer/researcher agent should address these before implementation begins.

### Gap 1: Embedding Strategy (SEMANTIC SEARCH)

Semantic search requires vector embeddings. Options:

| Option | Pros | Cons |
|--------|------|------|
| **sqlite-vec** + local embedding | No external deps, simple, SQLite-native | Embedding quality depends on local model; model size/performance |
| **External API** (OpenAI/text-embedding-3-small, etc.) | High quality, simple API, hosted | Cost per memory write, latency, external dependency |
| **Model's own embeddings** (LLM self-embeds) | No extra infrastructure, zero cost | Model-specific, may not be consistent, slower |
| **Hybrid** (keyword + semantic fallback) | Works without embeddings, graceful degradation | More complex implementation, two query paths |
| **Skip embeddings entirely** (keyword search on JSON) | Simplest first version, works today | No semantic understanding, misses related concepts |

**Open question**: What's the minimum viable semantic search? Can we ship without embeddings (keyword-only) and add them later?

**Example of the problem**: Agent stores memory "user dislikes TypeScript enums". Keyword search for "typescript" or "enums" finds it. But search for "alternative to const objects" would miss it without embeddings.

### Gap 2: Memory Model

What shape does a memory take?

| Model | Description | Complexity |
|-------|-------------|------------|
| **Flat key-value** | Simple key → value pairs. Like a dictionary. | Low |
| **Namespaced key-value** | `agent_id.key` → value. Partitioned by agent. | Low |
| **Tagged entries** | Memories with tags/labels for filtered retrieval. | Medium |
| **Episodic chunks** | Timestamped narrative memories (like a diary). | Medium |
| **Structured records** | Memories with typed fields (facts, preferences, patterns). | Medium |
| **Graph-based** | Memories as nodes with relationships. | High |

**Sub-question**: Do we distinguish memory types?
- **Semantic memory**: Facts ("the project uses React 18")
- **Episodic memory**: Past events ("on July 2, we tried approach X and it failed")
- **Procedural memory**: How to do things ("the deployment process has 3 steps")

Each type might need different storage and retrieval.

### Gap 3: Memory Forgetting and Consolidation

Do memories last forever? If not, how are they forgotten?

| Strategy | Mechanism | Complexity |
|----------|-----------|------------|
| **Explicit forget only** | Agents call `mem_forget`. Never auto-delete. | Minimal |
| **TTL-based** | Memories expire after a configurable TTL. | Low |
| **Importance-based eviction** | Low-importance memories deleted when storage is full. | Medium |
| **Consolidation** | Short-term → long-term pipeline (like hippocampal consolidation in brains). | High |
| **LRU eviction** | Least recently accessed memories deleted when storage is full. | Medium |

**Open question**: Do we need a forgetting mechanism at all for the first version? SQLite can handle millions of entries. TTL-based expiration is the simplest reasonable approach.

### Gap 4: Sharing Model

Who can read/write what?

| Model | What agents can see | What agents can write | Complexity |
|-------|--------------------|----------------------|------------|
| **Global shared** | All memories from all agents | Any agent writes to shared pool | Low |
| **Per-agent** | Only own memories | Only own namespace | Low |
| **Project-scoped** | Memories tagged with project ID | Agents in the project | Medium |
| **Capability-based** | Agents with matching capabilities | Agents with matching capabilities | Medium |
| **Hierarchical** | Agents can read down the hierarchy | Agents write to own level | High |

**Open question**: The simplest approach — shared memory with namespacing — effectively gives every agent access to everything. Is this acceptable? The user owns the whole system, so privacy between agents in the same user's session may be unnecessary complexity.

### Gap 5: Storage Architecture

SQLite-only, or SQLite + something else?

| Approach | Storage | Search | Complexity |
|----------|---------|--------|------------|
| SQLite only | JSON column for content | `LIKE`, FTS5, or JSON path queries | Low |
| SQLite + sqlite-vec | Same DB with vector extension | Semantic search via extension | Medium |
| SQLite + external vector DB | SQLite for metadata, Pinecone/Chroma for vectors | Full semantic search | High |

**Open question**: sqlite-vec (https://github.com/asg017/sqlite-vec) is the most promising option — it adds vector search as a SQLite extension, no separate infrastructure. Is it mature enough?

### Gap 6: Retrieval Strategy

When an agent searches memory, what happens?

| Strategy | Description | Complexity |
|----------|-------------|------------|
| **Keyword match** | FTS5 or simple string matching | Low |
| **Semantic search** | Vector similarity on query embedding | Medium |
| **Hybrid** | Both, with fusion of results | Medium |
| **RAG pipeline** | Retrieve → re-rank → summarize | High |

**Sub-question**: Should memory retrieval be synchronous (agent waits for search results) or can agents subscribe to memory changes?

## Research Needed Before Building

A researcher agent should investigate:

1. **sqlite-vec maturity**: Is it production-ready? What models does it support? Performance characteristics?
2. **Memory in existing agent systems**: How do Mem0, MemGPT, and similar projects handle these questions?
3. **Cost model**: For the external embedding API approach, what's the per-memory cost? Per-search cost?
4. **SQLite scaling**: At what point does the memory table need indexing beyond B-tree? What's the practical limit on entries?

## Minimal Viable Version (Suggestion)

If the research supports it, the simplest useful version:

- **Storage**: SQLite with FTS5 for keyword search (no embeddings yet)
- **Model**: Namespaced key-value with tags (flat, simple)
- **Sharing**: Global shared with namespace scoping per agent
- **Forgetting**: TTL-based expiration + explicit `mem_forget`
- **Tables**: `memory_entries(id, namespace, key, content_text, tags, metadata, created_at, expires_at, source_agent)`

This ships without semantic search, without embedding infrastructure, without consolidation. Semantic search can be added later (it's a backward-compatible extension to `mem_search`).

## Relevant Context

- **ARCHITECTURE.md** — plugin system design, memory is a built-in plugin
- **META.md** — vision for the Agora
- **projectmem** (arXiv:2606.12329) — structured memory for failure reflection
- This is an ideal candidate for a "research first, build second" approach — a researcher agent should produce a recommendation report before implementation
