"""Tool registry: in-memory store for discovered tools from upstream MCP servers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from fastmcp_gateway.tool_name import validate_tool_name

if TYPE_CHECKING:
    from fastmcp_gateway.access_policy import AccessPolicy

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("fastmcp_gateway.registry")


def infer_group(domain: str, tool_name: str) -> str:
    """Infer a tool's group from its name by stripping the domain prefix.

    Convention: tool names follow ``{domain}_{group}_{action}`` pattern.
    Examples:
        infer_group("apollo", "apollo_people_search")   -> "people"
        infer_group("hubspot", "hubspot_contacts_create") -> "contacts"
        infer_group("apollo", "search")                  -> "general"
    """
    prefix = f"{domain}_"
    if tool_name.startswith(prefix):
        remainder = tool_name[len(prefix) :]
        parts = remainder.split("_", 1)
        if parts[0]:
            return parts[0]
    return "general"


class ToolEntry(BaseModel):
    """A single tool in the registry."""

    model_config = ConfigDict(frozen=True)

    name: str
    domain: str
    group: str
    description: str
    input_schema: dict[str, Any]
    upstream_url: str
    original_name: str | None = None  # Set when renamed due to collision


class DomainInfo(BaseModel):
    """Summary information about a domain."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    groups: list[str]
    tool_count: int


class RegistryDiff(BaseModel):
    """Result of a populate/refresh operation, describing what changed."""

    model_config = ConfigDict(frozen=True)

    domain: str
    added: list[str]
    removed: list[str]
    tool_count: int


class ToolRegistry:
    """In-memory tool registry with domain/group organization.

    Stores tool metadata from upstream MCP servers and provides
    lookup, filtering, and search operations.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._domains: dict[str, dict[str, list[str]]] = {}  # domain -> group -> [tool_names]
        self._domain_descriptions: dict[str, str] = {}
        self._collided_names: set[str] = set()  # original names that had cross-domain collisions

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def register_tool(self, tool: ToolEntry) -> None:
        """Register a single tool, handling name collisions across domains.

        When two domains register tools with the same name, both are
        auto-prefixed with their domain name (``{domain}_{name}``) and
        the original name is preserved in :attr:`ToolEntry.original_name`.

        Upstream names are validated before registration. An unsafe
        name (one that would shadow a Python keyword or builtin inside
        the ``execute_code`` sandbox namespace, contains disallowed
        characters, or exceeds 64 chars) is rejected with a structured
        audit log; the registry is not mutated. Rejection is silent
        (no exception) so a single bad name in a populate batch does
        not abort the other tools from the same upstream.
        """
        reason = validate_tool_name(tool.name)
        if reason is not None:
            logger.warning(
                "Rejected tool registration: domain=%s name=%r reason=%s",
                tool.domain,
                tool.name,
                reason,
            )
            return

        existing = self._tools.get(tool.name)

        # Same-domain re-registration: always allow (simple update).
        # Must be checked before collision logic because _collided_names
        # would otherwise auto-prefix, which may fail if the prefixed name
        # is owned by another domain.
        if existing is not None and existing.domain == tool.domain:
            self._register_internal(tool)
            return

        # First collision: same name, different domain
        if existing is not None and existing.domain != tool.domain:
            existing_prefixed = f"{existing.domain}_{existing.name}"
            new_prefixed = f"{tool.domain}_{tool.name}"

            # Pre-flight: the two candidate prefixed names must both
            # pass ``validate_tool_name`` before any state mutation.
            # Without this check, a hyphenated domain (``sec-edgar``)
            # or a hostile domain (``Bad-Domain``) can push an invalid
            # synthesized name into the pipeline AFTER we've already
            # unregistered the existing tool and added to
            # ``_collided_names`` — the existing tool is then silently
            # lost and ``_collided_names`` carries a stray entry.
            # Validating up front lets us bail out atomically: either
            # the collision handling fully succeeds, or the registry
            # is untouched.
            check_blocker = self._tools.get(existing_prefixed)
            is_blocker_path = check_blocker is not None and check_blocker.domain != existing.domain
            candidates = [new_prefixed]
            if not is_blocker_path:
                # The blocker path never synthesizes existing_prefixed
                # (it keeps the existing tool under its current name),
                # so we only need to validate new_prefixed there.
                candidates = [new_prefixed, existing_prefixed]
            for candidate in candidates:
                reason = validate_tool_name(candidate)
                if reason is not None:
                    logger.warning(
                        "Tool name collision: aborting collision handling for "
                        "'%s' (domains '%s' vs '%s') — synthesized name %r "
                        "fails validation (%s); registry state unchanged",
                        tool.name,
                        existing.domain,
                        tool.domain,
                        candidate,
                        reason,
                    )
                    return

            # Check if the existing tool's prefixed name would collide with
            # a tool from yet another domain.  If so, we cannot safely
            # unregister the existing tool (it would be lost).  Instead,
            # keep the existing tool under its current name and only prefix
            # the new registrant.
            if is_blocker_path:
                # ``check_blocker`` is guaranteed non-None on this path
                # (``is_blocker_path`` requires it), so its ``domain``
                # is safe to dereference for the log line.
                blocker_domain = check_blocker.domain if check_blocker is not None else "?"
                logger.warning(
                    "Tool name collision: '%s' registered by both '%s' and '%s' "
                    "— cannot rename '%s' to '%s' (owned by domain '%s'), keeping original",
                    tool.name,
                    existing.domain,
                    tool.domain,
                    existing.name,
                    existing_prefixed,
                    blocker_domain,
                )
                self._collided_names.add(tool.name)
                self._register_internal(
                    tool.model_copy(
                        update={
                            "name": new_prefixed,
                            "original_name": tool.name,
                        }
                    )
                )
                return

            logger.warning(
                "Tool name collision: '%s' registered by both '%s' and '%s' — prefixing with domain names",
                tool.name,
                existing.domain,
                tool.domain,
            )
            self._collided_names.add(tool.name)

            # Remove existing tool and re-register with domain prefix
            self._unregister(existing.name)
            self._register_internal(
                existing.model_copy(
                    update={
                        "name": existing_prefixed,
                        "original_name": existing.original_name or existing.name,
                    }
                )
            )

            # Register new tool with domain prefix
            self._register_internal(
                tool.model_copy(
                    update={
                        "name": new_prefixed,
                        "original_name": tool.name,
                    }
                )
            )
            return

        # Name previously collided: auto-prefix any new registrant
        if tool.name in self._collided_names:
            self._register_internal(
                tool.model_copy(
                    update={
                        "name": f"{tool.domain}_{tool.name}",
                        "original_name": tool.name,
                    }
                )
            )
            return

        # No collision — normal registration
        self._register_internal(tool)

    def _register_internal(self, tool: ToolEntry) -> None:
        """Register a tool without collision detection (internal use).

        Validates ``tool.name`` one more time before the insertion into
        ``_tools``. ``register_tool`` already validates raw upstream
        names, but its collision paths synthesize new names
        (``f"{tool.domain}_{tool.name}"``) and hand them back to this
        method. A hostile or misconfigured domain string could push
        a synthesized name that fails the identifier gate through to
        the registry; this second check closes that bypass and
        preserves the invariant "every name in ``_tools`` is a safe
        Python identifier."
        """
        reason = validate_tool_name(tool.name)
        if reason is not None:
            logger.warning(
                "Rejected synthesized tool name: domain=%s name=%r reason=%s",
                tool.domain,
                tool.name,
                reason,
            )
            return

        old = self._tools.get(tool.name)
        if old is not None and old.domain != tool.domain:
            # Guard: refuse to silently overwrite a tool from another domain.
            # This can happen when collision prefixing produces a name that
            # matches an existing tool (e.g., domain "a_b" + tool "c" →
            # "a_b_c" colliding with domain "a"'s existing "b_c").
            logger.warning(
                "Cannot register tool '%s' (domain '%s'): name already used by domain '%s' — skipping",
                tool.name,
                tool.domain,
                old.domain,
            )
            return
        if old is not None and old.group != tool.group:
            self._remove_from_index(old.name, old.domain, old.group)

        self._tools[tool.name] = tool

        if tool.domain not in self._domains:
            self._domains[tool.domain] = {}
        if tool.group not in self._domains[tool.domain]:
            self._domains[tool.domain][tool.group] = []
        if tool.name not in self._domains[tool.domain][tool.group]:
            self._domains[tool.domain][tool.group].append(tool.name)

    def _unregister(self, tool_name: str) -> None:
        """Completely remove a tool from the registry."""
        tool = self._tools.pop(tool_name, None)
        if tool is not None:
            self._remove_from_index(tool_name, tool.domain, tool.group)

    def _remove_from_index(self, tool_name: str, domain: str, group: str) -> None:
        """Remove a tool name from the domain/group index."""
        if domain in self._domains and group in self._domains[domain]:
            names = self._domains[domain][group]
            if tool_name in names:
                names.remove(tool_name)
            # Clean up empty group
            if not names:
                del self._domains[domain][group]
            # Clean up empty domain
            if not self._domains[domain]:
                del self._domains[domain]

    def populate_domain(
        self,
        domain: str,
        upstream_url: str,
        tools: list[dict[str, Any]],
        *,
        description: str = "",
        group_overrides: dict[str, str] | None = None,
        policy: AccessPolicy | None = None,
    ) -> RegistryDiff:
        """Populate the registry with tools from an upstream server.

        Each tool dict should have at minimum ``name`` and ``inputSchema`` keys,
        matching the shape returned by MCP ``tools/list``.  An optional
        ``description`` key provides the tool's one-line summary.

        Groups are inferred from tool name prefixes unless overridden via
        *group_overrides* (mapping tool name -> explicit group).

        When *policy* is provided, tools rejected by
        :meth:`AccessPolicy.is_allowed` are skipped and never enter the
        registry -- callers of :meth:`lookup`, :meth:`search`, etc. will
        behave as if those tools don't exist.

        Returns a :class:`RegistryDiff` describing what changed.
        """
        with _tracer.start_as_current_span("gateway.registry.populate_domain") as span:
            span.set_attribute("gateway.domain", domain)

            # Snapshot current tool names for diff calculation.
            old_names = {t.name for t in self.get_tools_by_domain(domain)}

            self.clear_domain(domain)

            if description:
                self.set_domain_description(domain, description)

            overrides = group_overrides or {}
            filtered_count = 0
            prefix = f"{domain}_"
            for raw in tools:
                name: str = raw.get("name", "")
                if not name:
                    logger.warning("Skipping tool with empty name in domain %s", domain)
                    continue

                # Collision renaming (see register_tool) may rewrite this tool's
                # registered name to ``{domain}_{name}``.  Evaluate policy
                # against both forms so rules written in either shape apply to
                # the final registered name — a rule like
                # ``allowed_tools: ["crm_get_server_info"]`` works even when
                # the upstream advertises the tool bare as ``get_server_info``.
                if policy is not None:
                    prefixed_name = name if name.startswith(prefix) else prefix + name
                    if not policy.is_allowed(domain, prefixed_name, original_name=name):
                        filtered_count += 1
                        logger.debug("Tool '%s' in domain '%s' filtered by access policy", name, domain)
                        continue

                group = overrides.get(name, infer_group(domain, name))

                self.register_tool(
                    ToolEntry(
                        name=name,
                        domain=domain,
                        group=group,
                        description=raw.get("description", ""),
                        input_schema=raw.get("inputSchema", {}),
                        upstream_url=upstream_url,
                    )
                )
            if filtered_count > 0:
                span.set_attribute("gateway.policy_filtered_count", filtered_count)

            new_names = {t.name for t in self.get_tools_by_domain(domain)}
            diff = RegistryDiff(
                domain=domain,
                added=sorted(new_names - old_names),
                removed=sorted(old_names - new_names),
                tool_count=len(new_names),
            )
            span.set_attribute("gateway.tool_count", diff.tool_count)
            return diff

    def set_domain_description(self, domain: str, description: str) -> None:
        """Set a human-readable description for a domain."""
        self._domain_descriptions[domain] = description

    def get_domain_description(self, domain: str) -> str:
        """Return the description for a domain, or empty string if unset."""
        return self._domain_descriptions.get(domain, "")

    def clear_domain(self, domain: str) -> None:
        """Remove all tools for a domain (used during refresh)."""
        if domain in self._domains:
            for group_tools in self._domains[domain].values():
                for tool_name in group_tools:
                    self._tools.pop(tool_name, None)
            del self._domains[domain]
        self._domain_descriptions.pop(domain, None)

    def lookup(self, tool_name: str) -> ToolEntry | None:
        """Look up a tool by exact name."""
        return self._tools.get(tool_name)

    def get_domain_names(self) -> list[str]:
        """Get all registered domain names."""
        return sorted(self._domains.keys())

    def get_domain_info(self) -> list[DomainInfo]:
        """Get summary info for all domains."""
        result = []
        for domain_name in sorted(self._domains.keys()):
            groups = self._domains[domain_name]
            tool_count = sum(len(tools) for tools in groups.values())
            result.append(
                DomainInfo(
                    name=domain_name,
                    description=self._domain_descriptions.get(domain_name, ""),
                    groups=sorted(groups.keys()),
                    tool_count=tool_count,
                )
            )
        return result

    def get_tools_by_domain(self, domain: str) -> list[ToolEntry]:
        """Get all tools in a domain."""
        if domain not in self._domains:
            return []
        tool_names = []
        for group_tools in self._domains[domain].values():
            tool_names.extend(group_tools)
        return [self._tools[name] for name in sorted(tool_names) if name in self._tools]

    def get_tools_by_group(self, domain: str, group: str) -> list[ToolEntry]:
        """Get all tools in a specific domain/group."""
        if domain not in self._domains or group not in self._domains[domain]:
            return []
        tool_names = self._domains[domain][group]
        return [self._tools[name] for name in sorted(tool_names) if name in self._tools]

    def search(self, query: str) -> list[ToolEntry]:
        """Keyword search across tool names, original names, and descriptions.

        All whitespace-separated tokens must appear somewhere in the
        tool's name, original name, or description (AND semantics).
        """
        with _tracer.start_as_current_span("gateway.registry.search") as span:
            span.set_attribute("gateway.query", query)

            query_lower = query.lower()
            tokens = query_lower.split()
            results = []
            for tool in self._tools.values():
                searchable = f"{tool.name} {tool.original_name or ''} {tool.description}".lower()
                if all(token in searchable for token in tokens):
                    results.append(tool)
            results = sorted(results, key=lambda t: t.name)
            span.set_attribute("gateway.result_count", len(results))
            return results

    def get_all_tool_names(self) -> list[str]:
        """Get all registered tool names (for fuzzy matching)."""
        return sorted(self._tools.keys())

    def has_domain(self, domain: str) -> bool:
        return domain in self._domains

    def has_group(self, domain: str, group: str) -> bool:
        return domain in self._domains and group in self._domains[domain]

    def get_groups_for_domain(self, domain: str) -> list[str]:
        """Get group names for a domain."""
        if domain not in self._domains:
            return []
        return sorted(self._domains[domain].keys())
