"""Tests for the differentiated error response model on POST /registry/servers.

The endpoint maps add_upstream() failures into three response classes
so controller-style callers can disambiguate retry-worthy errors from
caller-fixable config errors from genuine internal failures:

  * 503 + Retry-After  — upstream unreachable or still starting (caller retries)
  * 422                — upstream auth/authz rejection (caller-fixable)
  * 502                — everything else (escalation-worthy), classified as
                         ``registration_failed`` with a one-line summary

Pre-fix, all three classes returned 500, leaving controllers unable
to differentiate startup-window noise from real problems.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fastmcp import FastMCP
from httpx2 import ASGITransport, AsyncClient
from mcp import MCPError
from mcp.types import CONNECTION_CLOSED

from fastmcp_gateway.errors import (
    _failure_identity,
    _failure_summary,
    _sanitize,
)
from fastmcp_gateway.gateway import (
    GatewayServer,
    _scrub_url_for_diagnostics,  # pyright: ignore[reportAttributeAccessIssue]
)

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REGISTRATION_TOKEN = "test-secret-token"

pytestmark = pytest.mark.filterwarnings("ignore:registration_token is deprecated:DeprecationWarning")


def _create_seed_server() -> FastMCP:
    mcp = FastMCP("seed-upstream")

    @mcp.tool()
    def seed_ping() -> str:
        """Echo a ping."""
        return json.dumps({"ok": True})

    return mcp


@pytest.fixture
def seed_server() -> FastMCP:
    return _create_seed_server()


@pytest.fixture
async def gateway(seed_server: FastMCP) -> GatewayServer:
    gw = GatewayServer(
        {"seed": seed_server},  # type: ignore[dict-item]
        registration_token=REGISTRATION_TOKEN,
    )
    await gw.populate()
    return gw


def _auth_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {REGISTRATION_TOKEN}"}


async def _client(gateway: GatewayServer) -> AsyncClient:
    app = gateway.mcp.http_app(transport="streamable-http")
    # raise_app_exceptions=False lets Starlette's default ASGI
    # exception handler turn an uncaught route exception into the
    # 500 the production server would return, instead of re-raising
    # back through the test transport. That mirrors what the
    # registry-controller actually sees on the wire.
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


# RFC 5737 TEST-NET-3 — guaranteed-non-routable public IP block used
# in documentation. Public-classification per the URL guard's CIDR
# check (i.e. not RFC1918, not loopback, not link-local, not metadata),
# so the guard accepts the registration without our needing to stub
# DNS resolution.
_PUBLIC_TEST_IP = "203.0.113.5"


async def _post_register(
    gateway: GatewayServer,
    *,
    domain: str = "widgets",
    url: str | None = None,
    discovery_url: str | None = None,
) -> httpx2.Response:
    """POST /registry/servers with a basic payload.

    Uses an RFC 5737 documentation IP so the URL guard passes without
    any DNS-resolution stubbing.
    """
    resolved_url = url or f"https://{_PUBLIC_TEST_IP}:8080/mcp"
    body: dict[str, str] = {"domain": domain, "url": resolved_url}
    if discovery_url is not None:
        body["discovery_url"] = discovery_url
    async with await _client(gateway) as http:
        return await http.post("/registry/servers", headers=_auth_headers(), json=body)


def _patch_add_upstream(gateway: GatewayServer, exc: BaseException) -> None:
    """Stub UpstreamManager.add_upstream to raise *exc* on next call."""

    async def _raiser(*_args: object, **_kwargs: object) -> None:
        raise exc

    gateway.upstream_manager.add_upstream = _raiser  # type: ignore[method-assign]


def _make_status_error(status_code: int) -> httpx2.HTTPStatusError:
    """Build an httpx2.HTTPStatusError carrying *status_code* on its response."""
    request = httpx2.Request("POST", f"https://{_PUBLIC_TEST_IP}:8080/mcp")
    response = httpx2.Response(status_code, request=request)
    return httpx2.HTTPStatusError("upstream rejected", request=request, response=response)


def _mcp_error(code: int) -> BaseException:
    """Build the MCP SDK's ``MCPError`` carrying JSON-RPC error *code*."""
    return MCPError(code=code, message="upstream answered with an error")


def _caused_by(outer: BaseException, cause: BaseException) -> BaseException:
    """Chain *cause* onto *outer* as an explicit ``__cause__`` (``raise outer from cause``)."""
    outer.__cause__ = cause
    return outer


def _raised_from(cause: BaseException) -> BaseException:
    """Wrap *cause* the way the fastmcp client reports a failed connection: ``RuntimeError`` raised from it."""
    return _caused_by(RuntimeError("client failed to connect"), cause)


# URLs inside a failure's message: each must be cut to ``scheme://host[:port]``,
# and none of the leaked parts may survive.
_URLS_IN_TEXT = [
    pytest.param(
        "https://svc:hunter2@upstream.example/mcp?token=sk-1",
        "https://upstream.example",
        ("svc", "hunter2", "/mcp", "token", "sk-1"),
        id="userinfo-path-query",
    ),
    pytest.param(
        "https://user:pass@[2001:db8::1]/mcp?token=secret",
        "https://[2001:db8::1]",
        ("user", "pass", "/mcp", "token", "secret"),
        id="ipv6-userinfo-path-query",
    ),
    pytest.param(
        "https://h.example/token-abc/mcp",
        "https://h.example",
        ("token-abc", "/mcp"),
        id="path-token",
    ),
    # An apostrophe, a parenthesis and a bracket are legal in a path, so none of
    # them may end the match and leave the rest of a token-bearing path behind.
    pytest.param(
        "https://h.example/token'abc/mcp?k=v",
        "https://h.example",
        ("token", "abc", "/mcp", "k=v"),
        id="path-apostrophe",
    ),
    pytest.param(
        "https://h.example/a(b)/token-xyz",
        "https://h.example",
        ("(b)", "token-xyz"),
        id="path-parenthesis",
    ),
    pytest.param(
        "https://h.example/a]b/token-xyz",
        "https://h.example",
        ("a]b", "token-xyz"),
        id="path-unmatched-bracket",
    ),
    # An upstream can quote a URL as JSON or percent-encoded. It is decoded
    # before the scan, so its userinfo and token-bearing path go too.
    pytest.param(
        r"https:\/\/user:password@h.example\/token-value",
        "https://h.example",
        ("user", "password", "token-value"),
        id="json-escaped",
    ),
    pytest.param(
        r"https:\\/\\/user:password@h.example\\/token-value",
        "https://h.example",
        ("user", "password", "token-value"),
        id="json-escaped-twice",
    ),
    pytest.param(
        r"https:\u002F\u002Fuser:password@h.example\u002Ftoken-value",
        "https://h.example",
        ("user", "password", "token-value"),
        id="json-unicode-escaped",
    ),
    pytest.param(
        "https%3A%2F%2Fuser%3Apassword%40h.example%2Ftoken-value",
        "https://h.example",
        ("user", "password", "token-value"),
        id="percent-encoded",
    ),
    pytest.param(
        r"https\u003a//user:password@h.example/token-value",
        "https://h.example",
        ("user", "password", "token-value"),
        id="json-escaped-colon",
    ),
    pytest.param(
        r"https://h.example/p?a=1\u0026token=SECRET",
        "https://h.example",
        ("SECRET",),
        id="json-escaped-ampersand-in-query",
    ),
    pytest.param(
        "postgresql://app:Xy7%20kLs3cr3t@db.internal:5432/app",
        "postgresql://db.internal:5432",
        ("Xy7", "kLs3cr3t"),
        id="percent-space-in-password",
    ),
    pytest.param(
        "postgresql://app:12345%2Fs3cr3t@db.internal/app",
        "postgresql://db.internal",
        ("12345", "s3cr3t"),
        id="percent-slash-in-password",
    ),
    pytest.param(
        "https://s3cr3t%3F@h.example/mcp",
        "https://h.example",
        ("s3cr3t",),
        id="percent-question-mark-in-userinfo",
    ),
    pytest.param(
        "https://ghp_tokenAsUser:x%23y@github.com/o/r",
        "https://github.com",
        ("ghp_tokenAsUser", "x%23y"),
        id="percent-hash-in-password",
    ),
    pytest.param(
        r"https://user:pw1\u0026zz9@h.example/mcp",
        "https://h.example",
        ("pw1", "zz9"),
        id="go-escaped-ampersand-in-password",
    ),
    pytest.param(
        "https%3A%2F%2Fs3cr3t%252F%40h.example%2Fmcp",
        "https://h.example",
        ("s3cr3t",),
        id="encoded-url-with-escaped-slash-in-userinfo",
    ),
    pytest.param(
        "redis://:hunter2@cache.example:6379/0",
        "redis://cache.example:6379",
        ("hunter2",),
        id="redis-password",
    ),
]


#: How a 502 body's ``error`` starts for the default registration of ``widgets``.
_FAILED_AT = f"Registering upstream 'widgets' at 'https://{_PUBLIC_TEST_IP}:8080' failed: "

# URLs framed by the punctuation of the text around them. The span reaches
# past the authority (an apostrophe or a parenthesis is legal in a path), so
# the scrubber must end the authority at the first character no host or port
# can hold, or the port is lost or the host keeps the punctuation.
_URLS_IN_PUNCTUATION = [
    pytest.param("for url 'https://upstream:8080' refused", "https://upstream:8080", id="quoted-with-port"),
    pytest.param("upstream (https://h.example) refused", "https://h.example", id="parenthesized"),
    pytest.param("see https://a.example:8443, then retry", "https://a.example:8443", id="comma-after-port"),
    pytest.param("at 'https://u:pw@[2001:db8::1]:8443' refused", "https://[2001:db8::1]:8443", id="quoted-ipv6"),
    # A period or colon after a numeric port ends the sentence, not the port.
    pytest.param("could not reach https://a.example:8443.", "https://a.example:8443", id="period-after-port"),
    pytest.param("from https://a.example:8443: connection reset", "https://a.example:8443", id="colon-after-port"),
    pytest.param(
        "from https://[2001:db8::1]:8443. retrying", "https://[2001:db8::1]:8443", id="period-after-ipv6-port"
    ),
    # A trailing dot after a hostname is part of it (an absolute name), so it stays.
    pytest.param("resolving https://h.example. failed", "https://h.example.", id="absolute-hostname"),
]

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLXZhbHVl"

# Failure messages carrying a credential, and the parts of it that must never
# reach a 502 body or an ERROR line.
_CREDENTIAL_MESSAGES = [
    pytest.param(
        "upstream refused the call: Authorization: Bearer abc.def.ghi", ("abc.def.ghi",), id="authorization-header"
    ),
    pytest.param("upstream refused bearer abc.def.ghi for this call", ("abc.def.ghi",), id="bearer"),
    pytest.param("upstream refused Basic dXNlcjpwYXNz", ("dXNlcjpwYXNz",), id="basic"),
    pytest.param(f"session {_JWT} expired", tuple(_JWT.split(".")), id="jwt"),
    pytest.param("bad query token=sk-123&page=2", ("sk-123",), id="token-equals"),
    pytest.param("bad query token%3Dsk-123%26page%3D2", ("sk-123",), id="token-percent-encoded"),
    pytest.param('upstream echoed {"password": "hunter2", "user": "u"}', ("hunter2",), id="json-password"),
    pytest.param("passwd: hunter2", ("hunter2",), id="passwd-colon"),
    pytest.param("CLIENT_SECRET=s3cr3t-value", ("s3cr3t-value",), id="client-secret-uppercase"),
    pytest.param(
        "x-api-key: k-123456 apikey=k-654321 access_key=AKIA123",
        ("k-123456", "k-654321", "AKIA123"),
        id="api-and-access-keys",
    ),
    pytest.param("signed url sig=deadbeef&signature=cafebabe", ("deadbeef", "cafebabe"), id="signatures"),
    pytest.param(
        "upstream refused: Cookie: session=abc123; csrftoken=xyz789", ("abc123", "xyz789"), id="cookie-header"
    ),
    pytest.param("upstream sent Set-Cookie: sid=s3ss10n; HttpOnly", ("s3ss10n",), id="set-cookie-header"),
    pytest.param(
        "secret_key=sk-live-1 PRIVATE_KEY: pk-2 credentials=cred-3 credential: cred-4",
        ("sk-live-1", "pk-2", "cred-3", "cred-4"),
        id="secret-private-key-and-credentials",
    ),
    pytest.param('upstream echoed {"accessToken": "abc"}', ("abc",), id="camel-case-key"),
    pytest.param("apiKey=k-1 clientSecret: s-2 refreshToken=r-3", ("k-1", "s-2", "r-3"), id="camel-case-keys"),
    pytest.param(r"upstream echoed {\"password\": \"hunter2\"}", ("hunter2",), id="escaped-json-password"),
    pytest.param(r"bad query a=1\u0026token=sk-9", ("sk-9",), id="escaped-ampersand-token"),
    pytest.param("password=p%26s3cr3t&user=x", ("s3cr3t",), id="encoded-delimiter-in-value"),
    pytest.param("Illegal header value b'sk-proj-abcdef0123456789SECRET\\n'", ("SECRET",), id="refused-header-value"),
    pytest.param("env: PGPASSWORD=hunter2 PGHOST=db", ("hunter2",), id="glued-password-key"),
    pytest.param(
        "apitoken=s3cr3tvalue authtoken: 2abcDEFs3cr3t", ("s3cr3tvalue", "2abcDEFs3cr3t"), id="glued-token-keys"
    ),
    pytest.param("Driver={ODBC};Server=db;Uid=sa;Pwd=hunter2;", ("hunter2",), id="odbc-pwd"),
    pytest.param(
        "passphrase=correct-horse sessionid=abc123def456", ("correct-horse", "abc123def456"), id="passphrase-sessionid"
    ),
    pytest.param(
        "AccountName=acct;AccountKey=bXlzZWNyZXRrZXk=;EndpointSuffix=x", ("bXlzZWNyZXRrZXk",), id="azure-account-key"
    ),
    pytest.param("Ocp-Apim-Subscription-Key: 0123456789abcdef0123456789abcdef", ("0123456789abcdef",), id="apim-key"),
    pytest.param("Invalid API key: k-live-abcdef0123456789", ("k-live-abcdef0123456789",), id="api-key-with-space"),
    pytest.param("rejected ghp_abcdefABCDEF0123456789abcdef012345", ("ghp_abcdef",), id="bare-github-token"),
    pytest.param("invalid_auth for xoxb-1234-5678-abcdefSECRET", ("abcdefSECRET",), id="bare-slack-token"),
    pytest.param("creds AKIAIOSFODNN7EXAMPLE in use", ("AKIAIOSFODNN7EXAMPLE",), id="bare-aws-key-id"),
    pytest.param("key AIzaSyA1234567890abcdefghijklmnopqrstu rejected", ("AIzaSyA1234567890",), id="bare-google-key"),
    pytest.param("{password => \"hunter2\"} and 'password' => 'zz9'", ("hunter2", "zz9"), id="hash-rockets"),
    pytest.param(
        "<password>hunter2</password><AccessToken>zz9abc</AccessToken>", ("hunter2", "zz9abc"), id="xml-elements"
    ),
    pytest.param("[(b'x-api-key', b's3cr3t-key')]", ("s3cr3t-key",), id="header-tuple"),
    pytest.param(
        "ran tool --token abc123secret --verbose and ['tool', '--password', 'zz9']",
        ("abc123secret", "zz9"),
        id="cli-flags",
    ),
    pytest.param(r'upstream echoed {"password": "ab\"zz9"}', ("zz9",), id="escaped-quote-in-value"),
    pytest.param(
        f'refused {_JWT} token=sk-123 {{"password": "hunter2"}} Authorization: Bearer abc.def.ghi',
        (*_JWT.split("."), "sk-123", "hunter2", "abc.def.ghi"),
        id="all-together",
    ),
]


# What a DEBUG traceback must never carry: a credential, URL userinfo, or a
# token-bearing path. The raising lines below hold them literally, since a
# frame shows its line's source, so the frames themselves must be sanitized.
_DEBUG_LEAKS = ("abc.def.ghi", *_JWT.split("."), "user:password", "password@", "token-value")


def _raise_upstream_refusal() -> None:
    """Raise the upstream's refusal, its message (and this line) carrying a JWT, userinfo and a credential."""
    raise ValueError(f"{_JWT} at https://user:password@example.com/mcp; Authorization: Bearer abc.def.ghi")


def _refusal_with_credentials() -> BaseException:
    """A raised client failure chained to a raised upstream refusal, both carrying credentials in their messages."""
    try:
        try:
            _raise_upstream_refusal()
        except ValueError as cause:
            raise RuntimeError(r"https:\/\/user:password@example.com\/token-value: Bearer x.y") from cause
    except RuntimeError as failure:
        return failure
    raise AssertionError("unreachable")


def _assert_registration_failed(response: httpx2.Response, *, error_count: int = 1) -> dict[str, object]:
    """Assert the classified 502 for a failure no other class covers, and return its body."""
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "registration_failed"
    assert body["domain"] == "widgets"
    assert body["error_count"] == error_count
    assert isinstance(body["error"], str)
    assert body["error"]
    assert "\n" not in body["error"]
    return body


def _assert_upstream_not_ready(
    response: httpx2.Response,
    *,
    upstream_status: int | None = None,
    upstream_error_code: int | None = None,
) -> None:
    """Assert the machine-readable 503 contract a retrying caller relies on.

    ``upstream_status`` and ``upstream_error_code`` are the discriminators
    that tell a slow start from a permanent misconfiguration: each must be
    present with the given value, or absent when ``None``.
    """
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    body = response.json()
    assert body["code"] == "upstream_not_ready"
    assert body["domain"] == "widgets"
    assert body["retry_after_seconds"] == 5
    for key, expected in (("upstream_status", upstream_status), ("upstream_error_code", upstream_error_code)):
        if expected is None:
            assert key not in body
        else:
            assert body[key] == expected


# ---------------------------------------------------------------------------
# Transient errors → 503 + Retry-After
# ---------------------------------------------------------------------------


class TestTransientErrorsMapTo503:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_cls",
        [
            httpx2.ConnectError,
            httpx2.ConnectTimeout,
            httpx2.ReadTimeout,
            httpx2.WriteTimeout,
            httpx2.PoolTimeout,
            httpx2.ReadError,
            httpx2.WriteError,
            httpx2.RemoteProtocolError,
        ],
    )
    async def test_returns_503_with_retry_after(self, gateway: GatewayServer, exc_cls: type[BaseException]) -> None:
        _patch_add_upstream(gateway, exc_cls("upstream not yet reachable"))

        response = await _post_register(gateway)

        assert response.status_code == 503
        assert "Retry-After" in response.headers
        retry_after = int(response.headers["Retry-After"])
        assert retry_after == 5

        body = response.json()
        assert body["code"] == "upstream_not_ready"
        assert body["domain"] == "widgets"
        assert body["retry_after_seconds"] == 5
        assert "upstream_status" not in body
        assert "upstream_error_code" not in body
        # Error message names the exception class so the operator can
        # disambiguate ConnectError from ReadTimeout without leaving
        # the response body.
        assert exc_cls.__name__ in body["error"]

    @pytest.mark.asyncio
    async def test_mcp_error_connection_closed_maps_to_503(self, gateway: GatewayServer) -> None:
        """``MCPError`` carrying ``CONNECTION_CLOSED`` (-32000) is a dropped session.

        The MCP SDK's ``MCPError`` is the unified wrapper for *any* error
        arriving over an MCP connection. The other codes an upstream answers
        with while it is still starting are covered by
        :class:`TestUpstreamStartupShapesMapTo503`; the codes that still mean
        a genuine failure by :class:`TestUnclassifiedErrorsMapTo500`.
        """
        _patch_add_upstream(gateway, MCPError(code=CONNECTION_CLOSED, message="upstream session dropped"))

        response = await _post_register(gateway)

        _assert_upstream_not_ready(response, upstream_error_code=CONNECTION_CLOSED)


# ---------------------------------------------------------------------------
# Upstream still starting → 503 + Retry-After
# ---------------------------------------------------------------------------


class TestUpstreamStartupShapesMapTo503:
    """Failures a registration probe meets while the upstream is still starting.

    A registry controller registers an upstream as soon as it is scheduled,
    so the first probe routinely lands before the upstream can answer
    ``tools/list``. Each shape here is boot-window noise the controller
    should retry after ``Retry-After``, not a 500 it escalates.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "code",
        [
            pytest.param(-32600, id="invalid-request"),
            pytest.param(-32601, id="method-not-found"),
            pytest.param(-32603, id="internal-error"),
            pytest.param(32600, id="sdk-v1-http-404-session-terminated"),
        ],
    )
    async def test_startup_mcp_error_codes_map_to_503(self, gateway: GatewayServer, code: int) -> None:
        _patch_add_upstream(gateway, _mcp_error(code))

        response = await _post_register(gateway)

        _assert_upstream_not_ready(response, upstream_error_code=code)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [404, 502, 503, 504])
    async def test_startup_http_statuses_map_to_503(self, gateway: GatewayServer, status_code: int) -> None:
        """404: the discovery route is not mounted yet. 502/503/504: nothing ready behind the proxy."""
        _patch_add_upstream(gateway, _make_status_error(status_code))

        response = await _post_register(gateway)

        _assert_upstream_not_ready(response, upstream_status=status_code)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("make_failure", "discriminators"),
        [
            pytest.param(lambda: _raised_from(httpx2.ConnectError("connection refused")), {}, id="refused-as-cause"),
            pytest.param(
                lambda: ExceptionGroup("probe failed", [httpx2.ConnectError("connection refused")]),
                {},
                id="refused-in-group",
            ),
            pytest.param(
                lambda: _raised_from(_make_status_error(503)), {"upstream_status": 503}, id="http-503-as-cause"
            ),
            pytest.param(
                lambda: _raised_from(_mcp_error(-32601)),
                {"upstream_error_code": -32601},
                id="method-not-found-as-cause",
            ),
        ],
    )
    async def test_wrapped_startup_failure_maps_to_503(
        self,
        gateway: GatewayServer,
        make_failure: Callable[[], BaseException],
        discriminators: dict[str, int],
    ) -> None:
        """The client can wrap the startup failure; a refused connection arrives as ``RuntimeError`` from it."""
        _patch_add_upstream(gateway, make_failure())

        response = await _post_register(gateway)

        _assert_upstream_not_ready(response, **discriminators)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("make_failure", "discriminators"),
        [
            pytest.param(lambda: _mcp_error(-32001), {"upstream_error_code": -32001}, id="read-deadline"),
            pytest.param(lambda: _mcp_error(408), {"upstream_error_code": 408}, id="sdk-v1-deadline-408"),
            pytest.param(lambda: _raised_from(TimeoutError()), {}, id="initialize-deadline-as-cause"),
        ],
    )
    async def test_client_side_timeout_maps_to_503(
        self,
        gateway: GatewayServer,
        make_failure: Callable[[], BaseException],
        discriminators: dict[str, int],
    ) -> None:
        """An upstream too slow to answer during startup is not ready yet.

        The MCP SDK v2 client reports a request whose read timeout expired as
        ``MCPError(-32001)``, an upstream proxying through an MCP SDK v1
        client passes that client's ``MCPError(408)`` back, and a missed
        ``initialize`` deadline arrives as ``RuntimeError`` raised from
        ``TimeoutError``.
        """
        _patch_add_upstream(gateway, make_failure())

        response = await _post_register(gateway)

        _assert_upstream_not_ready(response, **discriminators)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("make_failure", "upstream_status", "upstream_error_code"),
        [
            pytest.param(lambda: _make_status_error(404), 404, None, id="http-404"),
            pytest.param(lambda: _mcp_error(-32601), None, -32601, id="method-not-found"),
            pytest.param(lambda: _raised_from(httpx2.ConnectError("connection refused")), None, None, id="refused"),
        ],
    )
    async def test_not_ready_log_record_carries_the_discriminators(
        self,
        gateway: GatewayServer,
        caplog: pytest.LogCaptureFixture,
        make_failure: Callable[[], BaseException],
        upstream_status: int | None,
        upstream_error_code: int | None,
    ) -> None:
        """The log line an operator reads carries the same discriminators as the 503 body."""
        _patch_add_upstream(gateway, make_failure())

        with caplog.at_level(logging.INFO, logger="fastmcp_gateway.gateway"):
            response = await _post_register(gateway)

        _assert_upstream_not_ready(response, upstream_status=upstream_status, upstream_error_code=upstream_error_code)
        records = [
            record
            for record in caplog.records
            if record.name == "fastmcp_gateway.gateway" and hasattr(record, "upstream_error_code")
        ]
        assert len(records) == 1
        assert getattr(records[0], "upstream_status") == upstream_status  # noqa: B009
        assert getattr(records[0], "upstream_error_code") == upstream_error_code  # noqa: B009


# ---------------------------------------------------------------------------
# Upstream auth failures → 422
# ---------------------------------------------------------------------------


class TestUpstreamAuthErrorsMapTo422:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_returns_422_on_unauthorized_or_forbidden(self, gateway: GatewayServer, status_code: int) -> None:
        _patch_add_upstream(gateway, _make_status_error(status_code))

        response = await _post_register(gateway)

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "upstream_auth_failed"
        assert body["domain"] == "widgets"
        assert body["upstream_status"] == status_code
        # Stable contract: error key is present and non-empty.
        # Specific message copy is operator-facing prose that may
        # evolve without changing the machine-parseable contract
        # (code, status_code, domain, upstream_status).
        assert isinstance(body["error"], str)
        assert body["error"]

    @pytest.mark.asyncio
    async def test_wrapped_auth_rejection_returns_422(self, gateway: GatewayServer) -> None:
        """An auth rejection the client wraps is still caller-fixable, not a 500."""
        _patch_add_upstream(gateway, _raised_from(_make_status_error(403)))

        response = await _post_register(gateway)

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "upstream_auth_failed"
        assert body["upstream_status"] == 403

    @pytest.mark.asyncio
    async def test_auth_rejection_wrapping_a_startup_shape_returns_422(self, gateway: GatewayServer) -> None:
        """An auth rejection decides before a startup failure it wraps: still caller-fixable, not a 503."""
        _patch_add_upstream(gateway, _caused_by(_make_status_error(401), httpx2.ConnectError("connection refused")))

        response = await _post_register(gateway)

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "upstream_auth_failed"
        assert body["upstream_status"] == 401


# ---------------------------------------------------------------------------
# Non-classifiable failures → 502 registration_failed (escalation-worthy)
# ---------------------------------------------------------------------------


class TestUnclassifiedErrorsMapTo502:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 500])
    async def test_non_auth_upstream_status_falls_through_to_502(
        self, gateway: GatewayServer, status_code: int
    ) -> None:
        """An upstream status that is neither an auth rejection nor a startup shape is a genuine failure."""
        _patch_add_upstream(gateway, _make_status_error(status_code))

        response = await _post_register(gateway)

        # 422 is reserved for caller-fixable auth errors and 503 for an
        # upstream that is still starting. A genuine upstream 500, or a
        # request the upstream rejects outright, surfaces as a 502:
        # escalation-worthy rather than retried.
        body = _assert_registration_failed(response)
        assert body["upstream_status"] == status_code

    @pytest.mark.asyncio
    async def test_failure_raised_while_handling_a_refused_connection_falls_through_to_502(
        self, gateway: GatewayServer
    ) -> None:
        """Only an explicit cause is followed: a failure raised while handling a refused connection is its own."""
        failure = RuntimeError("registry corruption")
        failure.__context__ = httpx2.ConnectError("connection refused")
        _patch_add_upstream(gateway, failure)

        response = await _post_register(gateway)

        _assert_registration_failed(response)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make_failure",
        [
            pytest.param(lambda: _raised_from(_make_status_error(500)), id="http-500-as-cause"),
            pytest.param(
                lambda: _caused_by(_make_status_error(500), httpx2.ConnectError("connection refused")),
                id="http-500-raised-from-refused",
            ),
            pytest.param(
                lambda: _caused_by(_mcp_error(-32602), httpx2.ConnectError("connection refused")),
                id="invalid-params-raised-from-refused",
            ),
        ],
    )
    async def test_first_classifiable_failure_decides(
        self, gateway: GatewayServer, make_failure: Callable[[], BaseException]
    ) -> None:
        """A genuine failure stays a 502, whether the client wraps it or it wraps a startup shape itself.

        The first exception the walk can classify decides: an HTTP 500 or a
        request-shape ``MCPError`` is not turned into a 503 by a refused
        connection it was raised from.
        """
        _patch_add_upstream(gateway, make_failure())

        response = await _post_register(gateway)

        _assert_registration_failed(response)

    @pytest.mark.asyncio
    async def test_arbitrary_exception_falls_through_to_502(self, gateway: GatewayServer) -> None:
        """A bare RuntimeError is a genuine failure → 502."""
        _patch_add_upstream(gateway, RuntimeError("registry corruption"))

        response = await _post_register(gateway)
        _assert_registration_failed(response)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("code_name",),
        [
            ("INVALID_PARAMS",),  # -32602 — request shape mismatch
            ("PARSE_ERROR",),  # -32700 — peer couldn't parse our JSON
        ],
    )
    async def test_non_transient_mcp_error_codes_fall_through_to_502(
        self, gateway: GatewayServer, code_name: str
    ) -> None:
        """``MCPError`` codes that describe the gateway's own request are not retryable.

        A controller looping on ``INVALID_PARAMS`` or ``PARSE_ERROR`` would
        never make progress: an upstream that finishes starting rejects the
        same request the same way. They fall through to the 502 path so
        they surface as escalation-worthy rather than as a 503.
        """
        from mcp import types

        code = getattr(types, code_name)
        _patch_add_upstream(gateway, MCPError(code=code, message=f"peer reported {code_name}"))

        response = await _post_register(gateway)
        # 503 is reserved for an upstream that is still starting; 422 for
        # caller-fixable auth. A request-shape MCPError falls through to 502.
        _assert_registration_failed(response)

    @pytest.mark.asyncio
    async def test_the_answer_names_the_failure_without_its_message(
        self, gateway: GatewayServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The body carries no text from the message; the ERROR line carries its first line."""
        _patch_add_upstream(gateway, ValueError("1 validation error for ListToolsResult\ntools.0.inputSchema"))

        with caplog.at_level(logging.ERROR):
            response = await _post_register(gateway)

        body = _assert_registration_failed(response)
        assert body["error"] == f"{_FAILED_AT}ValueError"
        errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "ValueError: 1 validation error for ListToolsResult" in errors[0]
        assert "inputSchema" not in errors[0]

    @pytest.mark.asyncio
    async def test_the_outermost_failure_names_a_chain(self, gateway: GatewayServer) -> None:
        _patch_add_upstream(gateway, _raised_from(ValueError("bad listing")))

        response = await _post_register(gateway)

        body = _assert_registration_failed(response)
        assert body["error"].endswith("failed: RuntimeError")

    @pytest.mark.asyncio
    async def test_a_group_of_failures_is_counted(self, gateway: GatewayServer) -> None:
        failure = ExceptionGroup("probe failed", [ValueError("bad listing"), KeyError("name")])
        _patch_add_upstream(gateway, failure)

        response = await _post_register(gateway)

        body = _assert_registration_failed(response, error_count=2)
        assert body["error"].endswith("failed: ValueError; KeyError")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("message", "leaks"), _CREDENTIAL_MESSAGES)
    async def test_a_credential_in_the_failure_reaches_neither_the_answer_nor_the_log(
        self,
        gateway: GatewayServer,
        caplog: pytest.LogCaptureFixture,
        message: str,
        leaks: tuple[str, ...],
    ) -> None:
        """The body names only the failure's type; the ERROR line keeps the message with credentials redacted."""
        _patch_add_upstream(gateway, RuntimeError(message))

        with caplog.at_level(logging.ERROR):
            response = await _post_register(gateway)

        body = _assert_registration_failed(response)
        assert body["error"] == f"{_FAILED_AT}RuntimeError"
        errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "RuntimeError: " in errors[0]
        assert "[REDACTED]" in errors[0]
        for leak in leaks:
            assert leak not in errors[0], f"{leak!r} leaked into {errors[0]!r}"
            assert leak not in response.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("url", "host", "leaks"), _URLS_IN_TEXT)
    async def test_a_url_in_the_failure_is_cut_to_its_host(
        self,
        gateway: GatewayServer,
        caplog: pytest.LogCaptureFixture,
        url: str,
        host: str,
        leaks: tuple[str, ...],
    ) -> None:
        """The answer and the ERROR line name the failure's URL by scheme, host and port only."""
        _patch_add_upstream(gateway, RuntimeError(f"fetching {url} failed"))

        with caplog.at_level(logging.ERROR):
            response = await _post_register(gateway)

        body = _assert_registration_failed(response)
        assert body["error"].endswith(f"failed: RuntimeError ({host})")
        errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert f"fetching {host} failed" in errors[0]
        for text in (body["error"], errors[0]):
            for leak in leaks:
                assert leak not in text, f"{leak!r} leaked into {text!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cause_first", [True, False], ids=["cause-then-wrapper", "wrapper-then-cause"])
    async def test_a_cause_grouped_with_its_wrapper_is_one_failure(
        self, gateway: GatewayServer, cause_first: bool
    ) -> None:
        """An exception that is only another's explicit cause is part of that failure, wherever the group lists it."""
        cause = ValueError("bad listing")
        wrapper = _caused_by(RuntimeError("client failed to connect"), cause)
        members = [cause, wrapper] if cause_first else [wrapper, cause]
        _patch_add_upstream(gateway, ExceptionGroup("probe failed", members))

        response = await _post_register(gateway)

        body = _assert_registration_failed(response, error_count=1)
        assert body["error"].endswith("failed: RuntimeError")

    @pytest.mark.asyncio
    async def test_a_failure_logs_one_error_line_and_the_traceback_at_debug(
        self, gateway: GatewayServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A registry controller retries on every pass: each failed pass is one line, not a traceback."""
        _patch_add_upstream(gateway, RuntimeError("registry corruption\nsecond line"))

        with caplog.at_level(logging.DEBUG):
            response = await _post_register(gateway)

        _assert_registration_failed(response)
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "widgets" in errors[0].getMessage()
        assert "RuntimeError: registry corruption" in errors[0].getMessage()
        assert "\n" not in errors[0].getMessage()
        assert errors[0].exc_info is None
        assert "Traceback (most recent call last)" not in caplog.text
        frames = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
        assert len(frames) == 1
        assert "_raiser" in frames[0]

    @pytest.mark.asyncio
    async def test_the_debug_traceback_carries_frames_but_no_credential(
        self, gateway: GatewayServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """At DEBUG the failure is its summary, its exception types and its frames, all sanitized, never a raw message.

        The assertions read the formatted log text, which includes any
        traceback a handler would render, so a raw message, a credential, URL
        userinfo or a token-bearing path anywhere in the output fails them.
        """
        _patch_add_upstream(gateway, _refusal_with_credentials())

        with caplog.at_level(logging.DEBUG):
            response = await _post_register(gateway)

        _assert_registration_failed(response)
        for leak in _DEBUG_LEAKS:
            assert leak not in caplog.text, f"{leak!r} leaked into the log"
        debug = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
        assert len(debug) == 1
        for expected in (
            "[REDACTED]",
            "https://example.com",
            "RuntimeError",
            "ValueError",
            "_raiser",
            "_raise_upstream_refusal",
        ):
            assert expected in debug[0], f"{expected!r} missing from {debug[0]!r}"


# ---------------------------------------------------------------------------
# Transactional add_upstream: probe failure / refusal must not commit
# ---------------------------------------------------------------------------


class TestRegistrationIsTransactional:
    """``UpstreamManager.add_upstream`` rolls back on probe failure or refusal.

    Without these guarantees, a transient discovery error during an
    idempotent upsert would leave the manager pointing at the (unverified)
    new URL while the registry still carries the old tools — and
    ``execute_tool()`` would route to an upstream that never confirmed it
    was reachable.
    """

    @pytest.mark.asyncio
    async def test_transient_probe_failure_rolls_back_new_domain(self, gateway: GatewayServer) -> None:
        """Brand-new domain whose probe fails must not appear in ``list_upstreams``.

        Drives the real ``add_upstream`` path (no monkey-patch) so the
        rollback in ``UpstreamManager.add_upstream`` exercises end-to-end.
        We force the probe to fail by patching ``_prepare_domain`` on
        the manager instance.
        """

        async def _fail_populate(*_args: object, **_kwargs: object) -> object:
            raise httpx2.ConnectError("upstream not yet reachable")

        prior_domains = set(gateway.upstream_manager.list_upstreams().keys())
        gateway.upstream_manager._prepare_domain = _fail_populate  # type: ignore[method-assign]

        response = await _post_register(gateway, domain="brand-new")
        assert response.status_code == 503

        # The transient probe failure must NOT leave 'brand-new' visible
        # in the manager — list_upstreams() and execute_tool() both read
        # from the same internal dicts that add_upstream stages, so a
        # leaked entry would surface here.
        assert set(gateway.upstream_manager.list_upstreams().keys()) == prior_domains
        assert "brand-new" not in gateway.upstream_manager.domains

    @pytest.mark.asyncio
    async def test_transient_probe_failure_preserves_prior_url_on_upsert(
        self, gateway: GatewayServer, seed_server: FastMCP
    ) -> None:
        """Upsert: a failed probe for the new URL must leave the prior URL in place."""
        # Register a real upstream first to give the upsert a prior URL
        # to preserve. Drives the real add_upstream path.
        await gateway.upstream_manager.add_upstream("widgets", seed_server)  # type: ignore[arg-type]
        prior_url = gateway.upstream_manager.upstream_url("widgets")

        async def _fail_populate(*_args: object, **_kwargs: object) -> object:
            raise httpx2.ConnectError("v2 not yet reachable")

        gateway.upstream_manager._prepare_domain = _fail_populate  # type: ignore[method-assign]

        response = await _post_register(
            gateway,
            domain="widgets",
            url=f"https://{_PUBLIC_TEST_IP}:9090/v2/mcp",
        )
        assert response.status_code == 503

        # The rolled-back state must continue to expose the prior URL
        # so execute_tool() keeps routing where it did before the POST.
        assert gateway.upstream_manager.upstream_url("widgets") == prior_url

    @pytest.mark.asyncio
    async def test_refused_diff_returns_409_and_rolls_back(self, gateway: GatewayServer) -> None:
        """``diff.refused=True`` must surface as 409 without touching shared state.

        Schema-integrity refusals are caller-fixable (operator needs to
        explicitly acknowledge the new digest), so they're not 500 and
        not 503. The manager-side rollback ensures ``execute_tool()``
        continues to read the committed structures.
        """
        from fastmcp_gateway.registry import RegistryDiff

        async def _refused_add(*_args: object, **_kwargs: object) -> RegistryDiff:
            # Return a refused diff WITHOUT raising, mirroring what
            # registry.populate_domain does on schema-digest mismatch.
            return RegistryDiff(
                domain="widgets",
                added=[],
                removed=[],
                tool_count=0,
                schema_digest="0" * 64,
                schema_digest_changed=False,
                refused=True,
            )

        gateway.upstream_manager.add_upstream = _refused_add  # type: ignore[method-assign]

        response = await _post_register(gateway)

        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "schema_refused"
        assert body["domain"] == "widgets"


# ---------------------------------------------------------------------------
# Happy path is unchanged
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# URL scrubbing — error responses + logs name an upstream by scheme://host[:port] only
# ---------------------------------------------------------------------------


class TestScrubUrlForDiagnostics:
    """Unit tests for the scrub helper."""

    def test_drops_userinfo(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://user:secret@upstream.example/mcp")
        assert "user" not in scrubbed
        assert "secret" not in scrubbed
        assert scrubbed == "https://upstream.example"

    def test_drops_query_string(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://upstream.example/mcp?token=sk-deadbeef&signature=xyz")
        assert "token" not in scrubbed
        assert "sk-deadbeef" not in scrubbed
        assert scrubbed == "https://upstream.example"

    def test_drops_fragment(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://upstream.example/mcp#fragment-secret")
        assert "fragment-secret" not in scrubbed
        assert scrubbed == "https://upstream.example"

    def test_keeps_only_scheme_host_and_port(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://user:secret@upstream.example:8443/mcp?api_key=abc#frag")
        assert scrubbed == "https://upstream.example:8443"
        for leak in ("user", "secret", "/mcp", "api_key", "abc", "frag"):
            assert leak not in scrubbed

    def test_preserves_port(self) -> None:
        assert _scrub_url_for_diagnostics("http://upstream:8080/mcp") == "http://upstream:8080"

    @pytest.mark.parametrize(
        "url",
        ["https://h.example/token-abc/mcp", "https://h.example/some/deep/path", "https://h.example/"],
    )
    def test_drops_the_path(self, url: str) -> None:
        """A registered URL's path can carry a token, so no path survives."""
        assert _scrub_url_for_diagnostics(url) == "https://h.example"

    def test_unparseable_url_returns_placeholder(self) -> None:
        # Empty string has no scheme + no hostname → placeholder.
        assert _scrub_url_for_diagnostics("") == "<unparseable-url>"
        assert _scrub_url_for_diagnostics("not-a-url") == "<unparseable-url>"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param(
                "http://[::1]:8080/mcp",
                "http://[::1]:8080",
                id="ipv6-loopback-with-port",
            ),
            pytest.param(
                "https://[2001:db8::1]/mcp",
                "https://[2001:db8::1]",
                id="ipv6-no-port",
            ),
            pytest.param(
                "https://user:secret@[2001:db8::1]:8443/mcp?k=v#f",
                "https://[2001:db8::1]:8443",
                id="ipv6-with-userinfo-port-query-fragment",
            ),
        ],
    )
    def test_ipv6_hosts_keep_brackets(self, url: str, expected: str) -> None:
        """IPv6 hosts must be re-bracketed before being composed into netloc.

        ``urlsplit`` returns ``parts.hostname`` without brackets
        (``[::1]:80`` → ``"::1"``), so a naive ``f"{host}:{port}"``
        would yield ``"::1:80"`` — ambiguous to downstream parsers
        (the host could be ``::1:80`` with no port, or ``::1`` with
        port ``80``). The helper restores brackets for any host
        carrying a colon.
        """
        assert _scrub_url_for_diagnostics(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param("https://upstream:8080'", "https://upstream:8080", id="closing-quote-after-port"),
            pytest.param("https://h.example)", "https://h.example", id="closing-parenthesis"),
            pytest.param("https://h.example:8443,", "https://h.example:8443", id="comma-after-port"),
            pytest.param("https://u:p'w@h.example:8443)", "https://h.example:8443", id="userinfo-then-parenthesis"),
            pytest.param("https://[2001:db8::1]:8443'", "https://[2001:db8::1]:8443", id="ipv6-closing-quote"),
            pytest.param("https://h.example)/token-abc", "https://h.example", id="parenthesis-then-path"),
        ],
    )
    def test_punctuation_after_the_authority_is_not_part_of_it(self, url: str, expected: str) -> None:
        """A character no host or port can hold ends the authority: a quote or bracket of the text around a URL."""
        assert _scrub_url_for_diagnostics(url) == expected

    def test_invalid_port_treated_as_absent(self) -> None:
        """Non-numeric / out-of-range port must not raise.

        ``parts.port`` is a property that raises ``ValueError`` for
        strings like ``"abc"`` or numbers outside [0, 65535]. The
        scrubber's contract is to never raise on caller-supplied
        input, so the bad port is silently dropped and the URL is
        reconstructed without one.
        """
        assert _scrub_url_for_diagnostics("http://host:abc/mcp") == "http://host"
        assert _scrub_url_for_diagnostics("http://host:99999/mcp") == "http://host"


class TestFailureSummary:
    """Unit tests: a failure summary cuts every URL to ``scheme://host[:port]`` and counts a cause once."""

    @pytest.mark.parametrize(("url", "host", "leaks"), _URLS_IN_TEXT)
    def test_a_url_in_the_message_is_cut_to_its_host(self, url: str, host: str, leaks: tuple[str, ...]) -> None:
        summary, _ = _failure_summary(RuntimeError(f"fetching {url} failed"))

        assert f"fetching {host} failed" in summary
        for leak in leaks:
            assert leak not in summary, f"{leak!r} leaked into {summary!r}"

    def test_an_escaped_surrogate_decodes_to_text_that_encodes(self) -> None:
        """An escaped surrogate pair becomes its character; a lone surrogate becomes U+FFFD."""
        summary, _ = _failure_summary(RuntimeError(r"bad \ud83d\ude00 then \ud800 end"))

        assert summary == "RuntimeError: bad " + chr(0x1F600) + " then " + chr(0xFFFD) + " end"
        assert summary.encode("utf-8")

    @pytest.mark.parametrize(
        "text",
        [
            "https://user:pass@[2001:db8::1/mcp?token=secret",
            "https://h.example/items[0]/token-abc?token=secret",
        ],
        ids=["unclosed-ipv6-bracket", "bracket-in-path"],
    )
    def test_a_bracket_does_not_end_the_url_early(self, text: str) -> None:
        """A bracket inside a URL does not stop the match, so nothing after it survives unscrubbed."""
        summary, _ = _failure_summary(RuntimeError(f"fetching {text} failed"))

        for leak in ("user", "pass", "token", "secret", "/mcp", "items"):
            assert leak not in summary, f"{leak!r} leaked into {summary!r}"

    @pytest.mark.parametrize("cause_first", [True, False], ids=["cause-then-wrapper", "wrapper-then-cause"])
    def test_a_cause_grouped_with_its_wrapper_is_counted_once(self, cause_first: bool) -> None:
        cause = ValueError("bad listing")
        wrapper = _caused_by(RuntimeError("client failed to connect"), cause)
        members = [cause, wrapper] if cause_first else [wrapper, cause]

        summary, error_count = _failure_summary(ExceptionGroup("probe failed", members))

        assert error_count == 1
        assert summary == "RuntimeError: client failed to connect"
        assert _failure_identity(ExceptionGroup("probe failed", members)) == "RuntimeError"

    @pytest.mark.parametrize(("message", "leaks"), _CREDENTIAL_MESSAGES)
    def test_a_credential_in_the_message_is_redacted(self, message: str, leaks: tuple[str, ...]) -> None:
        summary, _ = _failure_summary(RuntimeError(message))

        assert summary.startswith("RuntimeError: ")
        for leak in leaks:
            assert leak not in summary, f"{leak!r} leaked into {summary!r}"

    @pytest.mark.parametrize(("text", "host"), _URLS_IN_PUNCTUATION)
    def test_punctuation_around_a_url_is_not_part_of_its_host(self, text: str, host: str) -> None:
        summary, _ = _failure_summary(RuntimeError(text))

        assert f"{host} " in summary or summary.endswith(host), summary

    def test_redaction_takes_only_the_credential(self) -> None:
        summary, _ = _failure_summary(RuntimeError("bad query token=sk-123&page=2"))

        assert summary == "RuntimeError: bad query token=[REDACTED]&page=2"

    def test_a_secret_named_key_is_redacted_only_as_a_whole_key(self) -> None:
        """``max_tokens`` or ``token_type`` name no secret, so their values stay."""
        summary, _ = _failure_summary(RuntimeError("max_tokens=100 token_type=bearer"))

        assert summary == "RuntimeError: max_tokens=100 token_type=bearer"


class TestFailureIdentity:
    """Unit tests: the 502 body names each failure by type, with its URLs' hosts, and no text from its message."""

    def test_a_failure_without_a_url_is_its_type(self) -> None:
        assert _failure_identity(RuntimeError("registry corruption: the secret is hunter2")) == "RuntimeError"

    @pytest.mark.parametrize(("url", "host", "leaks"), _URLS_IN_TEXT)
    def test_a_url_in_the_message_is_named_by_its_host(self, url: str, host: str, leaks: tuple[str, ...]) -> None:
        assert _failure_identity(RuntimeError(f"fetching {url} failed")) == f"RuntimeError ({host})"

    def test_each_distinct_host_is_named_once_in_order(self) -> None:
        failure = RuntimeError(
            "fetching https://user:pw@h.example:8443/mcp?token=x failed, see https://h.example/docs "
            "and https://h.example:8443/other"
        )

        assert _failure_identity(failure) == "RuntimeError (https://h.example:8443, https://h.example)"

    def test_each_failure_of_a_group_is_named(self) -> None:
        failure = ExceptionGroup("probe failed", [ValueError("bad https://a.example/x"), KeyError("name")])

        assert _failure_identity(failure) == "ValueError (https://a.example); KeyError"

    @pytest.mark.parametrize(("message", "leaks"), _CREDENTIAL_MESSAGES)
    def test_a_credential_never_appears(self, message: str, leaks: tuple[str, ...]) -> None:
        assert _failure_identity(RuntimeError(message)) == "RuntimeError"

    @pytest.mark.parametrize(("text", "host"), _URLS_IN_PUNCTUATION)
    def test_punctuation_around_a_url_is_not_part_of_its_host(self, text: str, host: str) -> None:
        assert _failure_identity(RuntimeError(text)) == f"RuntimeError ({host})"


def _messages_shaped_to_backtrack(size: int) -> list[Any]:
    """Messages of about *size* characters shaped to make a backtracking pattern retry at every position."""
    return [
        pytest.param("a" * size, id="letters-only"),
        pytest.param("a://" * (size // 4), id="schemes-only"),
        pytest.param("1://" * (size // 4), id="schemes-without-a-letter"),
        pytest.param("+" * (size - 4) + "://x", id="long-scheme-run"),
        pytest.param("token" * (size // 5), id="secret-keys-only"),
        pytest.param("eyJ" * (size // 3), id="jwt-prefixes-only"),
        pytest.param("Bearer " * (size // 7), id="bearer-words-only"),
        pytest.param("authorization" * (size // 13), id="authorization-words-only"),
        pytest.param("\\" * size, id="backslashes-only"),
        pytest.param("\\u" * (size // 2), id="unicode-escape-prefixes-only"),
        pytest.param("%" * size, id="percent-signs-only"),
        pytest.param("\\u0026" * (size // 6), id="escaped-ampersands-only"),
        pytest.param("%25" * (size // 3), id="escaped-percent-signs-only"),
        pytest.param('token: "' + "\\" * (size - 8), id="backslashes-in-a-quoted-value"),
        pytest.param("--" + "a" * (size - 2), id="one-long-flag"),
        pytest.param("AKIA" * (size // 4), id="aws-prefixes-only"),
        pytest.param("https://" + "\\u0026" * (size // 6), id="escapes-in-a-url"),
        pytest.param("apassword" * (size // 9), id="glued-secret-words-only"),
    ]


class _MessageThatRaises(Exception):
    def __str__(self) -> str:
        raise ValueError("cannot render")


class TestFailureMessageEdges:
    """The scan cap and an unrenderable message never expose a credential."""

    def test_the_cap_never_leaves_a_url_cut_inside_its_userinfo(self) -> None:
        """A URL the cap would cut mid-userinfo is dropped whole, so no part of the userinfo is named as a host."""
        filler = "x " * 2040
        failure = RuntimeError(f"{filler}https://ghp_abcdefab0123:pw@h.example/mcp")

        summary, _ = _failure_summary(failure)
        identity = _failure_identity(failure)

        for text in (summary, identity):
            assert "ghp_abcdefab" not in text
            assert "pw@" not in text

    def test_a_message_that_cannot_render_names_only_its_type(self) -> None:
        summary, count = _failure_summary(_MessageThatRaises())

        assert summary == "_MessageThatRaises"
        assert count == 1
        assert _failure_identity(_MessageThatRaises()) == "_MessageThatRaises"


class TestFailureSummaryBoundsTheWork:
    """A huge upstream error is summarized in linear time, on the event loop, without stalling it."""

    @pytest.mark.parametrize("message", _messages_shaped_to_backtrack(1_000_000))
    def test_a_huge_message_is_summarized_fast(self, message: str) -> None:
        """One megabyte: the input cap and the linear scanners keep this to milliseconds."""
        failure = RuntimeError(message)

        started = time.perf_counter()
        summary, _ = _failure_summary(failure)
        identity = _failure_identity(failure)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5, f"summarizing took {elapsed:.3f}s"
        assert len(summary) <= 300
        assert len(identity) <= 300

    @pytest.mark.parametrize("message", _messages_shaped_to_backtrack(100_000))
    def test_the_scanners_are_linear_without_the_cap(self, message: str) -> None:
        """The URL scanner and the redaction patterns are linear on their own, not only behind the input cap.

        A quadratic scan of 100,000 characters takes hours; a linear one takes
        tens of milliseconds, far inside the bound.
        """
        started = time.perf_counter()
        _sanitize(message)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5, f"scanning took {elapsed:.3f}s"


class TestRegistrationErrorResponseScrubsUrls:
    """Integration tests: the 503 / 422 response bodies must not echo secrets."""

    @pytest.mark.asyncio
    async def test_503_response_body_does_not_contain_userinfo_or_query(self, gateway: GatewayServer) -> None:
        _patch_add_upstream(gateway, httpx2.ConnectError("not yet reachable"))
        leaky_url = f"https://probe-user:probe-secret@{_PUBLIC_TEST_IP}:8080/mcp?token=sk-deadbeef"

        response = await _post_register(gateway, discovery_url=leaky_url)

        assert response.status_code == 503
        body_text = response.text
        for leak in ("probe-user", "probe-secret", "token=sk-deadbeef", "sk-deadbeef"):
            assert leak not in body_text, f"secret leaked into 503 body: {leak!r}"

    @pytest.mark.asyncio
    async def test_422_response_body_does_not_contain_userinfo_or_query(self, gateway: GatewayServer) -> None:
        _patch_add_upstream(gateway, _make_status_error(401))
        leaky_url = f"https://probe-user:probe-secret@{_PUBLIC_TEST_IP}:8080/mcp?token=sk-deadbeef"

        response = await _post_register(gateway, discovery_url=leaky_url)

        assert response.status_code == 422
        body_text = response.text
        for leak in ("probe-user", "probe-secret", "token=sk-deadbeef", "sk-deadbeef"):
            assert leak not in body_text, f"secret leaked into 422 body: {leak!r}"


class TestRegistrationDiagnosticsNameTheHostOnly:
    """Every registration diagnostic names the upstream by ``scheme://host[:port]``: a path can carry a token."""

    _URL_WITH_PATH_TOKEN = f"https://{_PUBLIC_TEST_IP}:8080/token-abc/mcp"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure", "status_code"),
        [
            pytest.param(httpx2.ConnectError("not yet reachable"), 503, id="503-not-ready"),
            pytest.param(_make_status_error(401), 422, id="422-auth-failed"),
            pytest.param(RuntimeError("registry corruption"), 502, id="502-registration-failed"),
        ],
    )
    async def test_the_answer_and_the_log_drop_the_path(
        self,
        gateway: GatewayServer,
        caplog: pytest.LogCaptureFixture,
        failure: BaseException,
        status_code: int,
    ) -> None:
        _patch_add_upstream(gateway, failure)

        with caplog.at_level(logging.DEBUG, logger="fastmcp_gateway.gateway"):
            response = await _post_register(gateway, url=self._URL_WITH_PATH_TOKEN)

        assert response.status_code == status_code
        assert f"'https://{_PUBLIC_TEST_IP}:8080'" in response.json()["error"]
        assert "token-abc" not in response.text
        messages = [record.getMessage() for record in caplog.records]
        assert any(f"https://{_PUBLIC_TEST_IP}:8080" in message for message in messages)
        for message in messages:
            assert "token-abc" not in message, f"path token leaked into log line {message!r}"


class TestSuccessfulRegistrationUnchanged:
    @pytest.mark.asyncio
    async def test_registration_returns_200_on_success(self, gateway: GatewayServer) -> None:
        """Successful add_upstream still returns the 200 + tools_discovered body.

        Regression coverage that the new try/except wrapping did not
        accidentally change the success-path response shape.
        """

        async def _ok_register(*_args: object, **_kwargs: object) -> object:
            from fastmcp_gateway.registry import RegistryDiff

            return RegistryDiff(domain="widgets", added=["new_tool"], removed=[], tool_count=1)

        gateway.upstream_manager.add_upstream = _ok_register  # type: ignore[method-assign]

        response = await _post_register(gateway)

        assert response.status_code == 200
        body = response.json()
        assert body["registered"] == "widgets"
        assert body["tools_discovered"] == 1
        assert body["tools_added"] == ["new_tool"]
