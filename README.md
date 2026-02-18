# fastmcp-gateway

Progressive tool discovery gateway for MCP. Built on [FastMCP](https://github.com/jlowin/fastmcp).

## The Problem

AI agents that connect to multiple MCP servers accumulate hundreds of tool definitions. This creates two scaling problems:

1. **Hard limits**: GPT-5.1 rejects >128 tools. Gemini has similar limits.
2. **Token overhead**: 200 tools = ~100K tokens of schema overhead per request, before the conversation starts.

Existing MCP gateways (MetaMCP, Microsoft MCP Gateway, IBM ContextForge) aggregate multiple servers behind a single endpoint — but they still send every tool to the LLM in one flat list.

## The Solution

`fastmcp-gateway` implements **progressive tool discovery**: instead of sending 200 tool schemas upfront, the gateway exposes 3 meta-tools that let the LLM discover and load tools on-demand.

```
Traditional:     Agent receives 200 tool schemas -> 100K tokens
Progressive:     Agent receives 3 meta-tools     ->   2K tokens
                 + loads schemas on demand        ->  +1K per tool
```

Based on [Speakeasy's research](https://www.speakeasy.com/blog/how-we-reduced-token-usage-by-100x-dynamic-toolsets-v2), this achieves **90-96% token reduction** while maintaining tool selection accuracy.

### Meta-Tools

| Tool | Purpose |
|------|---------|
| `discover_tools` | Browse tools by domain, group, or keyword. Returns names + descriptions, not schemas. |
| `get_tool_schema` | Get the full JSON Schema for a specific tool before calling it. |
| `execute_tool` | Execute any tool by name. Routes to the correct upstream server. |

## Status

**Early development.** We're building this in the open, starting from our production use case (221 tools across 8 MCP servers in an enterprise marketing agent).

See the [docs/](docs/) folder for research, requirements, and design decisions.

## Why Build Something New?

No existing project combines progressive tool discovery with an MCP gateway on FastMCP. See [docs/research/04-why-build-new.md](docs/research/04-why-build-new.md) for the full analysis.

## Research

- [Requirements](docs/requirements.md) — Functional and non-functional requirements
- [Gateway Landscape Analysis](docs/research/01-gateway-landscape.md) — Code-level review of MetaMCP, Microsoft MCP Gateway, IBM ContextForge
- [Progressive Discovery Prior Art](docs/research/02-progressive-discovery-prior-art.md) — Speakeasy, Claude Code Tool Search, and open-source implementations
- [FastMCP Building Blocks](docs/research/03-fastmcp-building-blocks.md) — What FastMCP already provides and what must be built
- [Why Build New](docs/research/04-why-build-new.md) — Why extending an existing gateway won't work
- [Community Anchors](docs/research/05-community-anchors.md) — MCP specs, Anthropic guidance, community best practices
- [Language and Architecture](docs/research/06-language-and-architecture.md) — Python confirmation, FastMCP extension architecture
- [PydanticAI Integration](docs/research/07-pydantic-ai-integration.md) — Integration points and technical requirements
- [Observability](docs/research/08-observability.md) — OpenTelemetry, MCP semantic conventions, Logfire
- [Connection Lifecycle](docs/research/09-connection-lifecycle.md) — Upstream connections and auth passthrough
- [Meta-Tool Schemas](docs/research/10-meta-tool-schemas.md) — API surface design for the 3 meta-tools

## License

Apache-2.0
