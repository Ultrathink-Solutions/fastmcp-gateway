"""Gateway server: the main entry point for fastmcp-gateway."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import warnings
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

import httpx
from fastmcp import FastMCP

from fastmcp_gateway.access_policy import AccessPolicy, normalize_upstreams
from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import HookRunner
from fastmcp_gateway.output_guard import OutputGuardConfig, OutputGuardHook
from fastmcp_gateway.registration_auth import (
    RegistrationAuthError,
    RegistrationTokenValidator,
)
from fastmcp_gateway.registry import ToolRegistry
from fastmcp_gateway.url_guard import (
    RegistrationGuardError,
    _url_guard_allow_private,
    validate_registration_headers,
    validate_registration_url,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.auth import AuthProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration-time error classification (POST /registry/servers)
# ---------------------------------------------------------------------------
# When ``add_upstream`` opens an MCP session to ``discovery_url`` and calls
# ``tools/list``, the probe can fail in three structurally different ways
# that controller-style callers need to disambiguate:
#
#   * Transient network failure  → 503 + Retry-After (caller retries)
#   * Upstream auth failure       → 422 (caller-fixable config error)
#   * Anything else              → 500 (genuine internal error, escalate)
#
# The tuples below name the concrete exception classes that map to each
# class. ``mcp.shared.exceptions.McpError`` is the SDK's unified wrapper
# for *any* error arriving over an MCP connection — both transport-class
# failures (``CONNECTION_CLOSED``) and peer-application protocol errors
# (``METHOD_NOT_FOUND``, ``INVALID_PARAMS``, ``INTERNAL_ERROR``, …).  We
# therefore do NOT treat it wholesale as transient; instead, the route
# handler inspects ``McpError.error.code`` per-call via
# :func:`_is_transient_mcp_error` and only retries on the JSON-RPC codes
# that unambiguously represent a dropped session.

# httpx errors raised when the upstream is unreachable or slow. These are
# the structural "upstream not yet Ready" failure modes a startup-window
# controller-side caller should retry on. Tuple form so ``except`` can
# pattern-match the whole class at once.
_UPSTREAM_TRANSIENT_HTTPX_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


# Class-import sentinel for ``mcp.shared.exceptions.McpError``. When the
# MCP SDK is available, the route handler catches McpError to decide
# transient-vs-fall-through based on the JSON-RPC error code carried in
# ``McpError.error.code``. When the SDK lays out its exception module
# differently in a future release, the tuple stays empty and the
# corresponding ``except`` clause matches nothing — leaving the
# httpx-only transient classification path load-bearing.
try:
    from mcp.shared.exceptions import McpError as _McpError  # type: ignore[import-not-found]

    _MCP_ERROR_TUPLE: tuple[type[BaseException], ...] = (_McpError,)
except ImportError:  # pragma: no cover — defensive fallback
    _MCP_ERROR_TUPLE = ()


# JSON-RPC error codes that the route handler treats as transient when
# wrapped in ``McpError``. ``CONNECTION_CLOSED`` (-32000) is the MCP-
# defined "session dropped" signal — the only code in the SDK's reserved
# range that unambiguously represents a transport-class failure. The
# rest of the JSON-RPC reserved range (-32700..-32600, plus -32603
# ``INTERNAL_ERROR``) covers protocol / peer-application errors that
# are NOT retry-actionable: a controller looping on ``METHOD_NOT_FOUND``
# or ``INVALID_PARAMS`` would never make progress, and ``INTERNAL_ERROR``
# is a peer-reported "I failed processing your request" that may or may
# not be transient — defaulting it to "fall through to 500" surfaces it
# as escalation-worthy rather than masking it as boot-window noise.
_TRANSIENT_MCP_ERROR_CODES: frozenset[int] = frozenset({-32000})


def _is_transient_mcp_error(exc: BaseException) -> bool:
    """Return ``True`` only for ``McpError`` instances carrying a transport-class code.

    ``mcp.shared.exceptions.McpError`` wraps both transport-class failures
    (e.g. ``CONNECTION_CLOSED`` when the session drops mid-flight) AND
    peer-application protocol errors (``METHOD_NOT_FOUND``,
    ``INVALID_PARAMS``, ``INTERNAL_ERROR``, …).  Only the former should
    map to 503; the latter are caller-fixable (or peer-fixable) and must
    fall through to the generic 500 path so a controller-side caller
    doesn't loop on an unrecoverable error.

    Returns ``False`` for any non-``McpError`` exception, or when the
    MCP SDK is unimportable (``_MCP_ERROR_TUPLE`` empty), or when the
    error's ``code`` is missing or not in the transient set.
    """
    if not isinstance(exc, _MCP_ERROR_TUPLE):
        return False
    error_data = getattr(exc, "error", None)
    if error_data is None:
        return False
    code = getattr(error_data, "code", None)
    return code in _TRANSIENT_MCP_ERROR_CODES


# Combined transient tuple used by the route handler's ``except`` clause.
# httpx transients are unconditionally transient; McpError needs a
# per-call code inspection (see :func:`_is_transient_mcp_error`).  When
# the MCP SDK is absent, ``_MCP_ERROR_TUPLE`` is empty and the combined
# tuple degenerates to the httpx-only set, preserving the previous
# behaviour exactly.
_UPSTREAM_TRANSIENT_OR_MCP_ERRORS: tuple[type[BaseException], ...] = (
    *_UPSTREAM_TRANSIENT_HTTPX_ERRORS,
    *_MCP_ERROR_TUPLE,
)


# httpx errors raised when the upstream returned a non-2xx response that
# carries an upstream-side status code we can inspect. We treat 401/403
# as caller-fixable config errors (422); other status codes fall through
# to the generic 500 path. ``httpx.HTTPStatusError`` is the only entry
# because every other status-carrying exception in httpx is a subclass.
_UPSTREAM_AUTH_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPStatusError,)


# Recommended retry delay (seconds) returned in the 503 body and as the
# ``Retry-After`` header value. Matches the registry-controller's default
# poll interval — a single missed cycle, then back to the normal rhythm.
_UPSTREAM_RETRY_AFTER_SECONDS = 5


def _extract_upstream_status_code(exc: BaseException) -> int | None:
    """Pull the upstream status code from an exception when one is attached.

    ``httpx.HTTPStatusError`` carries ``exc.response.status_code``;
    other exception shapes return ``None`` and the caller logs the
    type-name instead.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    return getattr(response, "status_code", None)


def _scrub_url_for_diagnostics(url: str) -> str:
    """Return *url* with userinfo, query, and fragment removed.

    Registration error responses and log lines echo the failing URL
    back to the caller so operators can disambiguate which upstream
    triggered the error. Some URL shapes carry secrets in those
    components (basic-auth in userinfo, signed-URL tokens in the
    query string), so we strip them before they reach a log
    aggregator or a JSON response body. Only ``scheme://host[:port]
    /path`` survives — enough to identify the upstream, never enough
    to replay against it.

    Malformed URLs that fail to parse fall back to the literal
    ``"<unparseable-url>"`` placeholder so we never leak the raw
    string by accident.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"

    if not parts.scheme or not parts.hostname:
        return "<unparseable-url>"

    host = parts.hostname
    # IPv6 hosts come back from ``urlsplit`` without their brackets
    # (``[::1]:80`` → host ``"::1"``). The bracketing must be
    # restored before composing netloc, or downstream parsers would
    # misread the embedded colons as port separators. Detect IPv6 by
    # the presence of a colon in the host string.
    bracketed_host = f"[{host}]" if ":" in host else host

    # ``parts.port`` accesses an underlying property that raises
    # ``ValueError`` when the URL carries a non-numeric or
    # out-of-range port string (e.g. ``http://h:abc/``). Treat that as
    # "no port" rather than letting the exception propagate — the
    # scrubber's contract is to never raise on caller-supplied input.
    try:
        port = parts.port
    except ValueError:
        port = None

    netloc = f"{bracketed_host}:{port}" if port is not None else bracketed_host

    # urlunsplit signature: (scheme, netloc, path, query, fragment)
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class CodeModeAuthorizerRequiredError(ValueError):
    """Raised when ``code_mode=True`` is set without an explicit authorizer.

    Subclasses :class:`ValueError` so existing callers catching the broad
    type keep working — but having a dedicated type lets callers (notably
    ``__main__`` / the CLI wrapper) route this specific misconfiguration
    to a user-friendly operator message without string-matching the error
    text, which is brittle across translations and copy edits.
    """


class GatewayServer:
    """Progressive tool discovery gateway for MCP.

    Aggregates tools from multiple upstream MCP servers and exposes them
    through 3 meta-tools: discover_tools, get_tool_schema, execute_tool.

    Parameters
    ----------
    upstreams:
        Mapping of domain name to upstream MCP server URL.  Values may also
        be object-shaped (``{"url": ..., "allowed_tools": [...], "denied_tools": [...]}``);
        the per-entry filters are collected into an :class:`AccessPolicy`
        unless an explicit *access_policy* is passed (which wins).
    name:
        Name for the FastMCP server instance.
    instructions:
        Custom LLM system instructions.  If ``None``, uses a sensible
        default describing the 3-step discovery workflow.
    registry_auth_headers:
        Headers to send to upstreams during startup registry population
        (runs outside any HTTP request context).
    upstream_headers:
        Per-domain headers for tool execution.  Domains listed here use
        these headers instead of request-passthrough.
    domain_descriptions:
        Human-readable descriptions for each domain, shown in the
        ``discover_tools`` domain summary.
    refresh_interval:
        If set, the gateway will periodically re-query all upstreams
        at this interval (in seconds) to keep the registry up-to-date.
        The background task runs inside the ASGI server lifespan.
    hooks:
        Optional list of hook instances for execution lifecycle callbacks.
        See :class:`~fastmcp_gateway.hooks.Hook` for the protocol.
    registration_token:
        Shared secret that protects the ``/registry/servers`` REST
        endpoints.  When set, the gateway exposes POST / DELETE / GET
        routes for dynamic upstream registration.  Callers must send
        ``Authorization: Bearer <token>``.  When ``None`` (default),
        the registration endpoints are **not** mounted.

        .. deprecated::
            The static-bearer path is retained for one release to
            give deployments a migration window; new deployments
            should use *registration_validator* with a short-lived
            signed JWT instead.  Constructing with
            *registration_token* emits a :class:`DeprecationWarning`.
        Mutually exclusive with *registration_validator*; setting
        both raises :class:`ValueError` at construction.
    registration_validator:
        Optional :class:`~fastmcp_gateway.registration_auth.RegistrationTokenValidator`
        that authenticates callers of ``/registry/servers``.  When
        set, the gateway exposes the same POST / DELETE / GET routes
        but delegates bearer validation to the validator — which
        typically verifies a short-lived signed JWT with issuer /
        audience / expiry claims, giving per-caller identity,
        automatic rotation, and an audit-log trail that a shared
        static bearer cannot provide.  When ``None`` (default) the
        static-bearer path (if *registration_token* is set) or
        no-registration behaviour applies.  Mutually exclusive with
        *registration_token*.
    access_policy:
        Optional :class:`AccessPolicy` applied to every registry population
        (startup, refresh, dynamic registration).  Tools rejected by the
        policy never enter the registry.  When ``None`` (default) and
        *upstreams* contains no per-entry filters, no filtering is applied.
        When both *access_policy* and per-entry filters are provided, the
        explicit *access_policy* wins (per-entry filters are ignored).
    code_mode:
        When ``True``, exposes an additional ``execute_code`` meta-tool
        that runs LLM-authored Python against the registered tools
        inside a Monty sandbox.  Experimental, off by default.  Requires
        the ``code-mode`` optional extra (``pip install
        "fastmcp-gateway[code-mode]"``).  See
        :mod:`fastmcp_gateway.code_mode` for safety notes.
    code_mode_authorizer:
        Async callback ``(user, context) -> bool`` that gates *each*
        ``execute_code`` call at session level.  Returning ``False``
        raises ``ExecutionDenied``.  **Required when** ``code_mode=True``
        — constructing a gateway with ``code_mode=True`` and no
        authorizer raises ``ValueError`` at init time.  This is
        deliberately stricter than previous releases: auto-discovery of
        an authorizer from the hook chain was removed because a hook
        whose authorizer always returned ``True`` could silently bypass
        the gate without any explicit opt-in.  Has no effect when
        ``code_mode=False``.
    code_mode_limits:
        Optional :class:`~fastmcp_gateway.code_mode.CodeModeLimits`
        overriding the default duration / memory / allocation / recursion /
        nested-call caps.  Has no effect when ``code_mode=False``.
    code_mode_audit_verbatim:
        When ``True``, raw LLM-authored code is emitted at DEBUG level
        in addition to the default hash+metadata INFO audit record.
        High-PII-risk; do not enable in production without review.
        Has no effect when ``code_mode=False``.
    middleware:
        Optional list of ASGI middleware to wrap the gateway's HTTP app
        with. Injected via ``FastMCP.http_app(middleware=...)``. When
        set, :meth:`GatewayServer.run` builds the ASGI app and runs it
        with uvicorn directly instead of delegating to
        ``FastMCP.run()``. HTTP-transport only; the behavior is
        backward-compat when left ``None``.

        Typical uses: host-allowlist filtering, request-id injection,
        rate limiting, CSP headers, structured-logging middleware. The
        middleware list is applied in declaration order (first entry
        outermost) — same convention as Starlette's ``Middleware`` stack.
    sanitizer_trusted_domains:
        Optional set of domain names whose tool descriptions will skip
        the registry-ingest **injection-pattern scan** only. Unicode
        normalization, control-character stripping, length cap, and
        inputSchema validation remain always-on. Use this for
        legitimate prompt-processing tools whose descriptions
        intentionally contain denylist tokens. Accepted as an explicit
        Python-code kwarg (no env-var form) so a deployment mistake
        can't silently weaken sanitation.
    output_guard:
        Optional :class:`~fastmcp_gateway.output_guard.OutputGuardConfig`
        enabling the gateway-level output guard. When ``enabled=True``,
        an :class:`~fastmcp_gateway.output_guard.OutputGuardHook` is
        **prepended** to the hook chain so it runs before any operator-
        supplied ``after_execute`` hooks — this guarantees downstream
        hooks see already-scrubbed output and an operator can't
        accidentally disable the guard by placing a non-compliant hook
        earlier in the list. Passed as a Python object (no env flag)
        so enabling / disabling requires a deliberate deployment
        change; default ``None`` means the guard is not installed.
    trusted_output_tools:
        Optional iterable of ``fnmatch`` glob patterns naming tools
        that are allowed to return prompt-like content — the output
        guard skips scrubbing for any registered tool whose name
        matches. Complementary to the upstream-declared
        ``annotations: {"x-raw-output-trusted": true}`` custom
        extension, which is the preferred signal; this operator-side
        override exists so a deployment can bypass scrubbing without
        coordinating with the upstream vendor. Patterns are applied
        at registry-populate time and re-applied on every refresh.
    auth:
        Optional :class:`fastmcp.server.auth.AuthProvider` for the inbound
        MCP client connection — passed straight to the underlying
        ``FastMCP(auth=...)`` constructor. Any ``AuthProvider`` subclass
        works: ``TokenVerifier`` for stateless JWT validation,
        ``RemoteAuthProvider`` for proxying to a remote authorization
        server, ``OAuthProvider`` / ``OAuthProxy`` for full OAuth 2.1 +
        DCR handling when the upstream IdP doesn't support RFC 7591
        Dynamic Client Registration. ``None`` (the default) leaves the
        FastMCP transport unauthenticated and matches prior behaviour.

    Usage::

        gateway = GatewayServer({
            "apollo": "http://apollo-mcp:8080/mcp",
            "hubspot": "http://hubspot-mcp:8080/mcp",
        })
        await gateway.populate()
        gateway.run()
    """

    def __init__(
        self,
        upstreams: dict[str, Any],
        *,
        name: str = "fastmcp-gateway",
        instructions: str | None = None,
        registry_auth_headers: dict[str, str] | None = None,
        upstream_headers: dict[str, dict[str, str]] | None = None,
        domain_descriptions: dict[str, str] | None = None,
        refresh_interval: float | None = None,
        hooks: list[Any] | None = None,
        registration_token: str | None = None,
        registration_validator: RegistrationTokenValidator | None = None,
        access_policy: AccessPolicy | None = None,
        code_mode: bool = False,
        code_mode_authorizer: Any | None = None,
        code_mode_limits: Any | None = None,
        code_mode_audit_verbatim: bool = False,
        middleware: list[Any] | None = None,
        sanitizer_trusted_domains: set[str] | None = None,
        output_guard: OutputGuardConfig | None = None,
        trusted_output_tools: set[str] | None = None,
        auth: AuthProvider | None = None,
    ) -> None:
        # Accept either a plain URL mapping or an object-shaped mapping with
        # per-entry allowed_tools / denied_tools.  The explicit access_policy
        # kwarg wins when both are provided.
        normalized_urls, inline_policy = normalize_upstreams(upstreams)
        effective_policy = access_policy if access_policy is not None else inline_policy

        self.upstreams = normalized_urls
        self.registry = ToolRegistry()
        self._domain_descriptions = domain_descriptions or {}
        self._custom_instructions = instructions  # None → auto-build from registry
        self._refresh_interval = refresh_interval
        self._refresh_task: asyncio.Task[None] | None = None
        # Build the hook list so the output guard (when enabled) is
        # **always first**. Prepending (vs appending) is deliberate:
        # operator-supplied ``after_execute`` hooks then see scrubbed
        # output, which is the right default. If we appended, a hook
        # that bailed early (returned without calling the next) could
        # silently skip sanitation — that contract inversion is the
        # kind of latent misconfig we explicitly refuse to allow.
        seeded_hooks: list[Any] = []
        self._output_guard_config = output_guard
        if output_guard is not None and output_guard.enabled:
            seeded_hooks.append(
                OutputGuardHook(
                    registry=self.registry,
                    mode=output_guard.mode,
                    max_scan_bytes=output_guard.max_scan_bytes,
                )
            )
        if hooks:
            seeded_hooks.extend(hooks)
        self._hook_runner = HookRunner(seeded_hooks)
        # Mutual exclusion between the deprecated static-bearer path
        # and the new validator path is enforced at construction so
        # that misconfigurations fail loudly at startup instead of
        # silently falling through to one path or the other — which
        # would be a footgun if an operator thought they had migrated
        # to the validator but the static token was still honoured.
        if registration_token is not None and registration_validator is not None:
            raise ValueError(
                "registration_token and registration_validator are mutually exclusive; "
                "pass only one (registration_validator is preferred — registration_token "
                "is deprecated and will be removed in a future release)."
            )
        if registration_token is not None:
            # One-release deprecation window.  Stacklevel=2 attributes
            # the warning to the caller constructing ``GatewayServer``
            # rather than to this module itself.
            warnings.warn(
                "registration_token is deprecated; pass a registration_validator "
                "(e.g. JWTRegistrationValidator) instead. The static-bearer path "
                "will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
        self._registration_token = registration_token
        self._registration_validator = registration_validator
        self._access_policy = effective_policy
        self._code_mode = code_mode
        # Require an explicit authorizer when code_mode is on.  Auto-discovery
        # from the hook chain was removed because it made the gate depend on
        # hook ordering / presence: a downstream hook that happened to expose
        # ``authorize_code_mode`` -> True would silently open the gate
        # without any explicit opt-in at the call site.  Callers that want
        # the old duck-typed discovery must now pass the hook's method
        # directly, e.g. ``code_mode_authorizer=my_hook.authorize_code_mode``.
        if code_mode and code_mode_authorizer is None:
            raise CodeModeAuthorizerRequiredError(
                "code_mode=True requires an explicit code_mode_authorizer "
                "callback. Pass one directly (e.g. "
                "code_mode_authorizer=my_hook.authorize_code_mode) — "
                "auto-discovery from the hook chain is no longer performed."
            )
        # Strict async check: a plain ``callable()`` test would accept a
        # synchronous function (e.g. ``lambda u, c: True``), which would
        # then blow up with ``TypeError: object bool can't be used in
        # 'await' expression`` at the first ``execute_code`` invocation
        # — a runtime-only landmine. ``inspect.iscoroutinefunction`` is
        # True for ``async def`` functions and async bound methods; fall
        # back to its ``__call__`` attribute so callable objects with an
        # async ``__call__`` are also accepted. (Python 3.14 deprecates
        # ``asyncio.iscoroutinefunction`` in favour of the ``inspect``
        # variant; we use ``inspect`` directly for forward compatibility.)
        #
        # Only validate the shape when ``code_mode`` is actually enabled
        # — the authorizer is dereferenced only inside the code-mode
        # execution path, so a sync authorizer passed alongside
        # ``code_mode=False`` is harmless (never invoked) and rejecting
        # it would needlessly penalise callers that pass the same
        # authorizer regardless of whether code mode is turned on for
        # a given construction.
        if (
            code_mode
            and code_mode_authorizer is not None
            and not (
                inspect.iscoroutinefunction(code_mode_authorizer)
                or inspect.iscoroutinefunction(
                    getattr(code_mode_authorizer, "__call__", None)  # noqa: B004
                )
            )
        ):
            raise TypeError(
                "code_mode_authorizer must be an async function "
                f"taking (user, context) -> bool; got {type(code_mode_authorizer).__name__}"
            )
        # Keep the stored type broad (Any | None) to match the declared
        # parameter shape — the runtime check above guarantees shape;
        # pyright's narrowing after ``iscoroutinefunction`` is too
        # restrictive for the downstream ``AuthorizerFn`` protocol.
        self._code_mode_authorizer: Any | None = code_mode_authorizer
        self._code_mode_limits = code_mode_limits
        self._code_mode_audit_verbatim = code_mode_audit_verbatim
        # ``middleware`` is routed through ``FastMCP.http_app`` at run
        # time, so store the caller's list verbatim. Shallow-copy to
        # prevent a post-construction mutation of the caller's list
        # from silently changing what runs on the server.
        self._middleware: list[Any] = list(middleware) if middleware else []
        self._registry_lock = asyncio.Lock()
        if registration_token and len(registration_token) < 16:
            logger.warning("GATEWAY_REGISTRATION_TOKEN is shorter than 16 characters — consider using a stronger token")
        self.upstream_manager = UpstreamManager(
            normalized_urls,
            self.registry,
            registry_auth_headers=registry_auth_headers,
            upstream_headers=upstream_headers,
            policy=effective_policy,
            sanitizer_trusted_domains=sanitizer_trusted_domains,
            trusted_output_tools=trusted_output_tools,
        )
        # ``auth`` plugs an inbound auth provider (TokenVerifier,
        # RemoteAuthProvider, OAuthProvider, or any other AuthProvider
        # subclass) into the underlying FastMCP server. When set, FastMCP
        # mounts the corresponding OAuth / discovery / DCR routes on the
        # HTTP transport — relevant for clients (Claude Code, Claude
        # Desktop, VS Code) that require RFC 7591 Dynamic Client
        # Registration against upstream identity providers that don't
        # speak DCR (e.g. Microsoft Entra ID).
        self._mcp = FastMCP(
            name,
            instructions=instructions if instructions is not None else self._default_instructions(),
            lifespan=self._server_lifespan if refresh_interval else None,
            auth=auth,
        )
        self._register_meta_tools()
        self._register_health_routes()
        # Routes are mounted whenever *either* authentication mode is
        # configured.  The ``_check_auth`` closure below picks the
        # right validator at request time.
        if registration_token or registration_validator is not None:
            self._register_registry_routes()

    @property
    def mcp(self) -> FastMCP:
        """Access the underlying FastMCP server instance."""
        return self._mcp

    @property
    def access_policy(self) -> AccessPolicy | None:
        """The effective :class:`AccessPolicy` in use, or ``None`` if unset.

        Reflects the resolved policy after considering both the explicit
        *access_policy* constructor argument and any inline filters parsed
        from object-shaped *upstreams* entries.
        """
        return self._access_policy

    @property
    def hook_runner(self) -> HookRunner:
        """Access the hook runner for advanced use."""
        return self._hook_runner

    def add_hook(self, hook: Any) -> None:
        """Register an execution lifecycle hook.

        Hooks are called in registration order.  See
        :class:`~fastmcp_gateway.hooks.Hook` for the protocol.
        """
        self._hook_runner.add(hook)

    async def populate(self) -> dict[str, int]:
        """Discover tools from all configured upstreams.

        Call this before serving requests so the registry is populated.
        Returns a mapping of domain -> tool count.
        """
        async with self._registry_lock:
            results = await self.upstream_manager.populate_all()
            self._apply_domain_descriptions()

            # Rebuild MCP instructions to include the domain summary so that
            # MCP clients see available domains during the initialization
            # handshake — no separate discover_tools() call required.
            self._update_instructions()

        return results

    def _apply_domain_descriptions(self) -> None:
        """Apply configured descriptions to domains currently in the registry.

        Called after both initial population and background refresh so that
        domains appearing late (e.g., a server that was down at startup but
        comes back during refresh) still receive their descriptions.
        """
        for domain, description in self._domain_descriptions.items():
            if self.registry.has_domain(domain):
                self.registry.set_domain_description(domain, description)
            else:
                logger.warning(
                    "Domain description for '%s' ignored — domain not populated",
                    domain,
                )

    def run(self, **kwargs: Any) -> None:
        """Run the gateway server.

        When the constructor received a non-empty ``middleware`` list,
        the ASGI app is built via ``FastMCP.http_app(middleware=...)``
        and served with uvicorn directly — the only path that lets
        caller-supplied middleware intercept requests before they
        reach the underlying FastMCP transport.

        When no middleware is configured, delegates to
        ``FastMCP.run()`` unchanged (backward-compat: this was the
        sole code path before the ``middleware`` kwarg existed).
        """
        if not self._middleware:
            self._mcp.run(**kwargs)
            return

        # Middleware wrapping is HTTP-transport-only. ``stdio`` and
        # ``sse`` don't build a Starlette stack, so there's nothing
        # to wrap; refuse loudly rather than silently drop the
        # middleware list.
        transport = kwargs.pop("transport", "streamable-http")
        if transport not in ("http", "streamable-http"):
            raise ValueError(
                f"middleware is only supported on HTTP transports "
                f"(http, streamable-http); got transport={transport!r}. "
                "Omit the middleware kwarg or switch to an HTTP transport."
            )

        host = kwargs.pop("host", "0.0.0.0")
        port = kwargs.pop("port", 8080)
        app = self._mcp.http_app(
            middleware=self._middleware,
            transport=transport,
        )
        # Defer the uvicorn import so consumers that never enable
        # middleware don't pay its import cost on stdio startup.
        import uvicorn

        uvicorn.run(app, host=host, port=port, **kwargs)

    # ------------------------------------------------------------------
    # Background refresh
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _server_lifespan(self, _app: FastMCP) -> AsyncIterator[None]:
        """ASGI lifespan that manages the background refresh task."""
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        yield
        self._refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._refresh_task
        self._refresh_task = None

    async def _refresh_loop(self) -> None:
        """Periodically re-query all upstreams to keep the registry fresh."""
        from opentelemetry import trace

        tracer = trace.get_tracer("fastmcp_gateway.gateway")
        assert self._refresh_interval is not None
        while True:
            await asyncio.sleep(self._refresh_interval)
            with tracer.start_as_current_span("gateway.background_refresh") as span:
                try:
                    async with self._registry_lock:
                        diffs = await self.upstream_manager.refresh_all()
                        span.set_attribute("gateway.domains_refreshed", len(diffs))
                        changed = False
                        for diff in diffs:
                            if diff.added or diff.removed:
                                changed = True
                                logger.info(
                                    "Registry refresh for '%s': +%d -%d tools",
                                    diff.domain,
                                    len(diff.added),
                                    len(diff.removed),
                                )
                        if changed:
                            self._apply_domain_descriptions()
                            self._update_instructions()
                except Exception:
                    span.set_attribute("gateway.refresh_failed", True)
                    logger.exception("Background registry refresh failed")

    # ------------------------------------------------------------------
    # Internal setup
    # ------------------------------------------------------------------

    def _register_meta_tools(self) -> None:
        """Register the meta-tools on the FastMCP server.

        The fourth meta-tool (``execute_code``) is only registered when
        ``code_mode=True`` was passed to the constructor.  When enabled,
        a :class:`~fastmcp_gateway.code_mode.CodeModeRunner` is
        constructed here so the ``pydantic-monty`` import is deferred
        until code mode is actually requested.
        """
        from fastmcp_gateway.meta_tools import register_meta_tools

        code_mode_runner = None
        if self._code_mode:
            from fastmcp_gateway.code_mode import CodeModeLimits, CodeModeRunner

            limits = self._code_mode_limits or CodeModeLimits()
            code_mode_runner = CodeModeRunner(
                self.registry,
                self.upstream_manager,
                self._hook_runner,
                limits=limits,
                authorizer=self._code_mode_authorizer,
                audit_verbatim=self._code_mode_audit_verbatim,
            )

        register_meta_tools(
            self._mcp,
            self.registry,
            self.upstream_manager,
            self._hook_runner,
            code_mode_runner=code_mode_runner,
        )

    def _register_health_routes(self) -> None:
        """Register /healthz and /readyz health check endpoints."""
        from opentelemetry import trace
        from starlette.responses import JSONResponse

        tracer = trace.get_tracer(__name__)
        registry = self.registry

        @self._mcp.custom_route("/healthz", methods=["GET"])
        async def _healthz(_request: Any) -> Any:
            with tracer.start_as_current_span("gateway.healthz") as span:
                span.set_attribute("http.method", "GET")
                span.set_attribute("http.route", "/healthz")
                return JSONResponse({"status": "ok"})

        @self._mcp.custom_route("/readyz", methods=["GET"])
        async def _readyz(_request: Any) -> Any:
            with tracer.start_as_current_span("gateway.readyz") as span:
                span.set_attribute("http.method", "GET")
                span.set_attribute("http.route", "/readyz")
                # Tool count remains observable via the OTel span for
                # operator telemetry, but is intentionally NOT emitted
                # in the response body. Kubernetes readiness probes
                # only consume the status code; any caller parsing
                # ``/readyz`` for subsystem state is a reconnaissance
                # signal we don't need to volunteer (how many tools a
                # gateway has routed tells an attacker the size of
                # the attack surface).
                span.set_attribute("registry.tool_count", registry.tool_count)
                return JSONResponse({"status": "ready"})

    def _register_registry_routes(self) -> None:
        """Register /registry/servers REST endpoints for dynamic upstream management.

        Mounted whenever *either* ``registration_token`` (legacy,
        deprecated) or ``registration_validator`` was provided at
        construction.  The per-request ``_check_auth`` closure picks
        the right validator: validator path first (preferred) then
        static-bearer fallback, then 401.
        """
        import json as _json

        from starlette.requests import Request  # noqa: TC002 - runtime use
        from starlette.responses import JSONResponse

        token = self._registration_token
        validator = self._registration_validator
        gateway = self  # Capture for closures.

        # Only build the constant-time comparison header when the
        # legacy path is active.  ``None`` here means "validator path
        # only" — the fallback branch in ``_check_auth`` just returns
        # 401.
        expected_header = f"Bearer {token}" if token else None

        def _check_auth(request: Request, *, route: str) -> JSONResponse | None:
            """Return an error response if the request is not authorized.

            Validator path is tried first when configured.  On success
            emits a structured audit log so the gateway has a record
            of the authenticated principal per registration event.
            Static-bearer path uses ``hmac.compare_digest`` to keep
            timing-side-channel resistance.
            """
            auth = request.headers.get("authorization", "")
            if validator is not None:
                try:
                    claims = validator.validate(auth)
                except RegistrationAuthError:
                    return JSONResponse(
                        {"error": "Unauthorized", "code": "unauthorized"},
                        status_code=401,
                    )
                # Audit fields are emitted both in the message body (so
                # they show up under the default CLI formatter, which
                # only renders ``%(message)s`` and ignores ``extra``
                # attributes) and via ``extra=`` (so structured handlers
                # — JSON/OTEL log shippers — still see them as separate
                # record attributes).  ``jti`` is optional in the claims
                # payload; render ``"-"`` in the text form when absent
                # rather than the Python literal ``None``.
                logger.info(
                    "registry.auth.ok subject=%s jti=%s iat=%s route=%s",
                    claims.subject,
                    claims.jti or "-",
                    claims.issued_at.isoformat(),
                    route,
                    extra={
                        "subject": claims.subject,
                        "jti": claims.jti,
                        "iat": claims.issued_at.isoformat(),
                        "route": route,
                    },
                )
                return None
            if expected_header is not None:
                if not hmac.compare_digest(auth, expected_header):
                    return JSONResponse(
                        {"error": "Unauthorized", "code": "unauthorized"},
                        status_code=401,
                    )
                return None
            # Neither path configured — refuse by default.  Routes
            # shouldn't be mounted in this case, but belt-and-
            # suspenders: a future refactor that mounts routes
            # unconditionally should not silently permit unauth'd
            # access.
            return JSONResponse(
                {"error": "No registration authentication configured", "code": "unauthorized"},
                status_code=401,
            )

        @self._mcp.custom_route("/registry/servers", methods=["POST"])
        async def _register_server(request: Request) -> JSONResponse:
            auth_err = _check_auth(request, route="register")
            if auth_err:
                return auth_err

            try:
                body = await request.json()
            except _json.JSONDecodeError:
                return JSONResponse(
                    {"error": "Invalid JSON body", "code": "bad_request"},
                    status_code=400,
                )

            domain = body.get("domain")
            url = body.get("url")
            if not domain or not url:
                return JSONResponse(
                    {"error": "'domain' and 'url' are required", "code": "bad_request"},
                    status_code=400,
                )
            if not isinstance(domain, str) or not isinstance(url, str):
                return JSONResponse(
                    {"error": "'domain' and 'url' must be strings", "code": "bad_request"},
                    status_code=400,
                )

            description = body.get("description")
            headers = body.get("headers")
            discovery_url = body.get("discovery_url")
            if discovery_url is not None and not isinstance(discovery_url, str):
                return JSONResponse(
                    {"error": "'discovery_url' must be a string", "code": "bad_request"},
                    status_code=400,
                )
            if headers is not None and (
                not isinstance(headers, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
            ):
                return JSONResponse(
                    {"error": "'headers' must be an object of string:string pairs", "code": "bad_request"},
                    status_code=400,
                )

            # SSRF + header-injection guards.  These run after the basic
            # type checks so the failure modes stay in a predictable
            # order for callers.  Both raise ``RegistrationGuardError``
            # with an explicit ``code`` attribute that we surface as the
            # structured ``code`` field in the 400 response.
            try:
                await validate_registration_url(
                    url,
                    allow_private=_url_guard_allow_private(),
                )
                # discovery_url targets a separate endpoint (e.g.
                # `/_introspect`) on the same upstream host. Pass through
                # the same SSRF/private-network guards as the execution
                # URL — there is no scenario where a discovery URL should
                # be allowed to a destination class the execution URL is
                # not. Skip when unset to preserve backward-compat.
                if discovery_url is not None:
                    await validate_registration_url(
                        discovery_url,
                        allow_private=_url_guard_allow_private(),
                    )
                if headers is not None:
                    validate_registration_headers(headers)
            except RegistrationGuardError as exc:
                return JSONResponse(
                    {"error": str(exc), "code": exc.code},
                    status_code=400,
                )

            # Differentiated error response model (#NN).  ``add_upstream``
            # opens an MCP session to ``discovery_url`` and calls
            # ``tools/list`` — that probe can fail in three structurally
            # different ways that a controller-style caller needs to
            # disambiguate:
            #
            #   * Transient network failure (upstream pod still booting,
            #     intermittent DNS, transport-level timeout).  Caller
            #     should retry on the next reconcile cycle.  We return
            #     ``503 Service Unavailable`` + ``Retry-After`` so the
            #     caller backs off briefly and tries again without
            #     escalating log severity.
            #   * Upstream authentication / authorization failure (401 /
            #     403 from the discovery probe).  Caller-fixable config
            #     error — retrying won't help until the operator
            #     corrects the auth posture.  We return ``422
            #     Unprocessable Entity`` so the caller logs once and
            #     stops hammering the endpoint.
            #   * Anything else — actual internal error.  Falls through
            #     to the generic ``500`` path with the original
            #     exception logged.
            #
            # Reserving 500 for true internal errors lets controllers
            # treat 5xx-but-503 as escalation-worthy and 503 as expected
            # boot-window noise.
            try:
                # Probe the upstream OUTSIDE the registry lock. The
                # ``add_upstream`` call opens an MCP session to
                # ``discovery_url`` and awaits ``tools/list`` —
                # holding ``gateway._registry_lock`` across that I/O
                # would serialize every concurrent registration
                # against an arbitrarily slow upstream. The asyncio
                # event loop is single-threaded, and
                # ``add_upstream`` is documented as an idempotent
                # upsert for same-domain re-registration; the
                # manager's per-domain dict mutations and
                # ``ToolRegistry.populate_domain`` are both
                # ``await``-free synchronous regions that the event
                # loop cannot interleave, so this remains safe
                # without external serialization for the probe.
                diff = await gateway.upstream_manager.add_upstream(
                    domain,
                    url,
                    discovery_url=discovery_url,
                    headers=headers,
                    registry_auth_headers=headers,
                )
                # Schema-integrity gate refusal. ``add_upstream``
                # rolls back its per-domain dict mutations on this
                # path, so the manager continues to point at the
                # prior URL/clients and ``execute_tool()`` keeps
                # routing where it did before this POST. Do NOT
                # touch description metadata or rebuild instructions
                # — those would commit operator-visible state for a
                # refusal that the registry just preserved.
                if diff.refused:
                    safe_url = _scrub_url_for_diagnostics(discovery_url or url)
                    logger.warning(
                        "Registration refused for domain '%s' (%s): "
                        "upstream schema digest changed since the prior registration",
                        domain,
                        safe_url,
                    )
                    return JSONResponse(
                        {
                            "error": (
                                f"Registration refused for '{domain}': the upstream's "
                                "tool schema changed since the prior registration. "
                                "Re-register with an explicit expected_digest "
                                "acknowledging the new shape."
                            ),
                            "code": "schema_refused",
                            "domain": domain,
                        },
                        status_code=409,
                    )
                # Re-acquire the registry lock only for the cross-
                # cutting registry mutations that ``add_upstream``
                # does NOT perform (description metadata + global
                # description/instruction rebuild). These read the
                # registry holistically and must observe a
                # consistent snapshot.
                async with gateway._registry_lock:
                    if description:
                        gateway.registry.set_domain_description(domain, description)
                    gateway._apply_domain_descriptions()
                    gateway._update_instructions()
            except _UPSTREAM_TRANSIENT_OR_MCP_ERRORS as exc:
                # ``McpError`` is the SDK's unified wrapper for both
                # transport-class failures and peer-application
                # protocol errors. Re-raise the non-transient flavour
                # so it falls through to the generic 500 path — a
                # controller looping on ``METHOD_NOT_FOUND`` would
                # never make progress. httpx transients (the rest of
                # the matched tuple) are unconditionally transient.
                if isinstance(exc, _MCP_ERROR_TUPLE) and not _is_transient_mcp_error(exc):
                    raise
                # Scrub userinfo / query / fragment off the URL before
                # it lands in logs or in the response body — some URL
                # shapes carry secrets in those components.
                safe_url = _scrub_url_for_diagnostics(discovery_url or url)
                logger.info(
                    "Upstream '%s' (%s) not yet reachable: %s — returning 503 for retry",
                    domain,
                    safe_url,
                    type(exc).__name__,
                )
                return JSONResponse(
                    {
                        "error": (
                            f"Upstream '{domain}' is not yet reachable at "
                            f"'{safe_url}' ({type(exc).__name__}). "
                            "This is expected during pod startup; retry shortly."
                        ),
                        "code": "upstream_not_ready",
                        "domain": domain,
                        "retry_after_seconds": _UPSTREAM_RETRY_AFTER_SECONDS,
                    },
                    status_code=503,
                    headers={"Retry-After": str(_UPSTREAM_RETRY_AFTER_SECONDS)},
                )
            except _UPSTREAM_AUTH_ERRORS as exc:
                upstream_status = _extract_upstream_status_code(exc)
                # Only 401/403 from the upstream maps to a caller-fixable
                # auth-config error. Other status codes (404 missing
                # endpoint, 5xx upstream-internal, etc.) are not
                # actionable by a controller-side retry policy — fall
                # through to the generic 500 handler so they surface as
                # escalation-worthy rather than as "stop hammering this".
                if upstream_status not in (401, 403):
                    raise
                safe_url = _scrub_url_for_diagnostics(discovery_url or url)
                logger.warning(
                    "Upstream '%s' (%s) rejected discovery probe with status %s — caller-fixable",
                    domain,
                    safe_url,
                    upstream_status,
                )
                return JSONResponse(
                    {
                        "error": (
                            f"Upstream '{domain}' rejected the discovery probe at "
                            f"'{safe_url}' with status "
                            f"{upstream_status}. The auth posture between the "
                            "gateway and the upstream is misconfigured; this will "
                            "not succeed on retry without operator action."
                        ),
                        "code": "upstream_auth_failed",
                        "domain": domain,
                        "upstream_status": upstream_status,
                    },
                    status_code=422,
                )

            return JSONResponse(
                {
                    "registered": domain,
                    "url": url,
                    "discovery_url": discovery_url or url,
                    "tools_discovered": diff.tool_count,
                    "tools_added": diff.added,
                }
            )

        @self._mcp.custom_route("/registry/servers/{domain}", methods=["DELETE"])
        async def _deregister_server(request: Request) -> JSONResponse:
            auth_err = _check_auth(request, route="deregister")
            if auth_err:
                return auth_err

            domain = request.path_params.get("domain", "")
            if not domain:
                return JSONResponse(
                    {"error": "'domain' path parameter is required", "code": "bad_request"},
                    status_code=400,
                )

            async with gateway._registry_lock:
                try:
                    removed = await gateway.upstream_manager.remove_upstream(domain)
                except KeyError:
                    return JSONResponse(
                        {"error": f"Domain '{domain}' is not registered", "code": "not_found"},
                        status_code=404,
                    )
                gateway._update_instructions()

            return JSONResponse(
                {
                    "deregistered": domain,
                    "tools_removed": removed,
                }
            )

        @self._mcp.custom_route("/registry/servers", methods=["GET"])
        async def _list_servers(request: Request) -> JSONResponse:
            auth_err = _check_auth(request, route="list")
            if auth_err:
                return auth_err

            async with gateway._registry_lock:
                upstreams = gateway.upstream_manager.list_upstreams()
                servers = []
                for domain, url in sorted(upstreams.items()):
                    tools = gateway.registry.get_tools_by_domain(domain)
                    servers.append(
                        {
                            "domain": domain,
                            "url": url,
                            "tool_count": len(tools),
                            "description": gateway.registry.get_domain_description(domain),
                        }
                    )
            return JSONResponse({"servers": servers, "total": len(servers)})

        @self._mcp.custom_route("/registry/servers/refresh", methods=["POST"])
        async def _refresh_server(request: Request) -> JSONResponse:
            """Operator-triggered, digest-acknowledged domain refresh.

            Background refreshes refuse any populate that diverges from
            the stored per-domain digest.  This endpoint is the
            explicit escape path: the operator independently verifies
            the new upstream schema, computes the expected post-
            transition digest, and presents it as a query parameter.
            On match the transition commits; on mismatch 409 Conflict
            is returned and registry state is preserved.

            There is deliberately no env-flag bypass.  Env toggles on
            integrity paths create a permanent latent "off" state that
            an attacker with process-env write access can flip; the
            per-call query-param shape forces every transition to be
            an intentional, audited operator action.
            """
            auth_err = _check_auth(request, route="refresh")
            if auth_err:
                return auth_err

            try:
                body = await request.json()
            except _json.JSONDecodeError:
                return JSONResponse(
                    {"error": "Invalid JSON body", "code": "bad_request"},
                    status_code=400,
                )

            domain = body.get("domain")
            if not domain or not isinstance(domain, str):
                return JSONResponse(
                    {"error": "'domain' (string) is required in body", "code": "bad_request"},
                    status_code=400,
                )

            # Validate expected_digest query param: must be present and
            # must be a 64-char lowercase hex string (the shape of a
            # SHA-256 hexdigest).  Reject anything else up-front — a
            # malformed digest can never match a real stored digest, so
            # forwarding it to the registry would just surface as a
            # generic 409; a 400 here is the correct, specific error.
            expected_digest = request.query_params.get("expected_digest")
            if expected_digest is None:
                return JSONResponse(
                    {
                        "error": "'expected_digest' query parameter is required",
                        "code": "bad_request",
                    },
                    status_code=400,
                )
            if len(expected_digest) != 64 or any(c not in "0123456789abcdef" for c in expected_digest):
                return JSONResponse(
                    {
                        "error": "'expected_digest' must be a 64-char lowercase hex string",
                        "code": "bad_request",
                    },
                    status_code=400,
                )

            async with gateway._registry_lock:
                try:
                    diff = await gateway.upstream_manager.refresh_domain(
                        domain,
                        expected_digest=expected_digest,
                    )
                except KeyError:
                    return JSONResponse(
                        {"error": f"Domain '{domain}' is not registered", "code": "not_found"},
                        status_code=404,
                    )

                if diff.refused:
                    computed = diff.schema_digest or ""
                    return JSONResponse(
                        {
                            "error": "digest_mismatch",
                            "code": "conflict",
                            "expected": expected_digest[:8] + "...",
                            "computed": (computed[:8] + "...") if computed else "",
                            "domain": domain,
                        },
                        status_code=409,
                    )

                gateway._apply_domain_descriptions()
                gateway._update_instructions()

            return JSONResponse(
                {
                    "refreshed": domain,
                    "tool_count": diff.tool_count,
                    "added": diff.added,
                    "removed": diff.removed,
                    "schema_digest": diff.schema_digest,
                    "schema_digest_changed": diff.schema_digest_changed,
                }
            )

    def _update_instructions(self) -> None:
        """Rebuild MCP instructions from the current registry state.

        Skipped when the caller supplied custom *instructions* at construction
        time — those take precedence and are never overwritten.
        """
        if self._custom_instructions is not None:
            return
        self._mcp.instructions = self._build_instructions()

    def _build_instructions(self) -> str:
        """Build instructions that include the domain summary from the registry.

        The MCP spec's ``instructions`` field in ``InitializeResult`` is the
        primary mechanism for a server to communicate high-level context to
        the LLM during the handshake — before the client calls ``tools/list``.
        Including the domain summary here means any MCP client immediately
        knows what tool domains are available without a separate discovery step.
        """
        domain_info = self.registry.get_domain_info()
        if not domain_info:
            return self._default_instructions()

        lines = [
            "You have access to a tool discovery gateway with tools across these domains:\n",
        ]
        for info in domain_info:
            desc = f" \u2014 {info.description}" if info.description else ""
            lines.append(f"- **{info.name}** ({info.tool_count} tools){desc}")

        lines.append("")
        lines.append(
            "Workflow: discover_tools() \u2192 get_tool_schema() \u2192 execute_tool()\n"
            'Use `discover_tools(domain="...")` to see tools in a specific domain.\n'
            "Skip discovery for tools you've already used in this conversation."
        )
        return "\n".join(lines)

    @staticmethod
    def _default_instructions() -> str:
        return (
            "You have access to a tool discovery gateway with 3 tools:\n"
            "1. discover_tools - Browse available tools. Call with no arguments to see domains, "
            "or with a domain to see specific tools.\n"
            "2. get_tool_schema - Get a tool's parameter schema before using it.\n"
            "3. execute_tool - Run any discovered tool.\n"
            "Workflow: discover_tools -> get_tool_schema -> execute_tool. "
            "Skip discovery for tools you've already used in this conversation."
        )
