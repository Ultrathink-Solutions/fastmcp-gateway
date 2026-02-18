# Progressive Tool Discovery: Prior Art

**Date:** 2026-02-17
**Status:** Research complete

---

## What Is Progressive Tool Discovery?

Instead of sending all tool schemas to the LLM upfront (consuming 80-100K+ tokens), progressive discovery exposes a small set of **meta-tools** that let the LLM discover and load tool schemas on-demand:

```
Traditional:     Agent receives 200 tool schemas -> 105K tokens overhead
Progressive:     Agent receives 3 meta-tools -> 2K tokens initial
                 Agent discovers relevant tools -> +1-2K per tool loaded
```

The pattern was first described by [Speakeasy](https://www.speakeasy.com/blog/how-we-reduced-token-usage-by-100x-dynamic-toolsets-v2) and has since been implemented in various forms by multiple projects.

---

## Production Implementations

### Speakeasy Dynamic Toolsets v2

- **Blog**: [How we reduced token usage by 100x](https://www.speakeasy.com/blog/how-we-reduced-token-usage-by-100x-dynamic-toolsets-v2)
- **Comparison**: [Progressive Discovery vs Semantic Search](https://www.speakeasy.com/blog/100x-token-reduction-dynamic-toolsets)
- **Platform**: [Gram](https://github.com/speakeasy-api/gram) (TypeScript, AGPL-3.0, 211 stars)

**The foundational research.** Speakeasy demonstrated that tool schemas represent 60-80% of token usage, and deferring schema loading is the key insight.

**Three meta-tools:**
1. `search_tools` -- Embeddings-based semantic search with categorical overviews
2. `describe_tools` -- Retrieves detailed schemas on demand
3. `execute_tool` -- Executes discovered tools by name

**Performance**: 96.7% input token reduction (simple tasks), 91.2% (complex tasks). 100% success rates maintained across 40-400 tools. Trade-off: 2-3x more tool calls, ~50% longer execution time on first tool use.

**Benchmarks** (400-tool server):

| Approach | Tokens per request | Tools visible |
|----------|-------------------|---------------|
| Static (all tools) | 405K | 400 |
| Progressive discovery | 6K | 3 meta-tools |
| Semantic search | 5K | 3 meta-tools |

**Availability**: **Proprietary cloud feature.** The Gram open-source repo (AGPL-3.0) contains zero code for `search_tools`, `describe_tools`, or `dynamic_toolset`. Dynamic toolsets are enabled via the Gram dashboard -- a hosted cloud feature, not available as a standalone library.

---

### Claude Code MCP Tool Search

- **Shipped**: January 14, 2026 (Claude Code v2.1.7)
- **Feature request**: [anthropics/claude-code#7336](https://github.com/anthropics/claude-code/issues/7336)
- **Explainer**: [Claude Code Finally Gets Lazy Loading for MCP Tools](https://blog.stackademic.com/claude-code-finally-gets-lazy-loading-for-mcp-tools-explained-39b613d1d5cc)

**Implementation**:
1. Detection: When MCP tool descriptions exceed 10K tokens, tools are marked `defer_loading: true`
2. Meta-tool injection: Claude receives a "Tool Search" tool instead of all definitions
3. Search modes: Regex-based pattern matching and BM25 semantic similarity
4. Selective loading: ~3-5 matching tools (~3K tokens) per query

**Performance**: 85% reduction (77K -> 8.7K tokens with 50+ tools). Improved tool selection accuracy from 79.5% to 88.1%.

**Availability**: **Client-locked.** Built into Claude Code only. Not available as a reusable component for other MCP clients or agent frameworks.

---

## Open-Source Implementations

| Project | Language | Stars | Approach | Maturity |
|---------|----------|-------|----------|----------|
| [lazy-mcp](https://github.com/voicetreelab/lazy-mcp) | Go | 71 | Hierarchical tree navigation (2 meta-tools) | Early production |
| [pmcp](https://github.com/ViperJuice/pmcp) | Python | 2 | 4-layer disclosure (11 meta-tools) | Very early |
| [MCP-Zero](https://github.com/xfey/MCP-Zero) | Python | 446 | Hierarchical semantic routing | Academic research |
| [tool-gating-mcp](https://github.com/ajbmachon/tool-gating-mcp) | Python | 6 | FAISS + sentence transformers | Experimental |
| [HF Progressive Disclosure](https://huggingface.co/spaces/MCP-1st-Birthday/mcp-extension-progressive-disclosure) | Python | N/A | Two-stage via MCP resources | PoC / demo |

### lazy-mcp (Go, MIT, 71 stars)

The most production-oriented open-source option. Two meta-tools: `get_tools_in_category(path)` for hierarchical tree navigation and `execute_tool(tool_path, arguments)` for execution. Implements lazy MCP server connections.

**Limitations**: Simple tree navigation (not semantic search). No schema-deferred loading. No Python/FastMCP integration.

### pmcp (Python, MIT, 2 stars)

Progressive gateway with 11 meta-tools across 4 disclosure layers. Includes dynamic server provisioning from a manifest.

**Limitations**: Very early-stage. 11 meta-tools may increase LLM cognitive load.

### MCP-Zero (Python, MIT, 446 stars)

Academic paper implementation. Hierarchical semantic routing across 308 servers / 2,797 tools. Claims 98% token reduction.

**Limitations**: Research code, not production-ready.

---

## MCP Specification Status

Progressive discovery is **not part of the MCP specification**. Active proposals:

| Proposal | Status | Approach |
|----------|--------|----------|
| [Discussion #532](https://github.com/orgs/modelcontextprotocol/discussions/532) | Early-stage | `tools/categories`, `tools/discover`, `tools/load`, `tools/unload` |
| [SEP #1888](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1888) | Draft (no sponsor) | Single meta-tool with operations/types modes |

All proposals have minimal community traction. None are close to adoption.

---

## Key Takeaway

**No mature, general-purpose, production-ready progressive discovery layer exists as an open-source library.**

The two production implementations are proprietary (Speakeasy) or client-locked (Claude Code). The open-source attempts are either Go-based (no Python/FastMCP integration), academic research, or very early-stage.

This is the white space `fastmcp-gateway` fills.
