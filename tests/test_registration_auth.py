"""Tests for ``fastmcp_gateway.registration_auth``.

Covers the JWT validator in isolation (happy path, wrong issuer,
wrong audience, expired, malformed, prefix handling) plus a full
route-integration test that constructs ``GatewayServer`` with a
validator and drives ``/registry/servers`` end-to-end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastmcp import FastMCP
from httpx import ASGITransport, AsyncClient

from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.registration_auth import (
    JWTRegistrationValidator,
    RegistrationAuthError,
)

# ---------------------------------------------------------------------------
# Test fixtures: ES256 keypair + JWT minting helpers
# ---------------------------------------------------------------------------

ISSUER = "https://issuer.test/oauth"
AUDIENCE = "https://gateway.test/registry/servers"


@pytest.fixture(scope="module")
def ec_keypair() -> tuple[str, str]:
    """Generate an ephemeral EC P-256 keypair (PEM-encoded)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


def _mint_jwt(
    private_pem: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    subject: str = "registrar-bot",
    exp_delta: timedelta = timedelta(minutes=5),
    include_exp: bool = True,
    include_iat: bool = True,
    include_jti: bool = True,
    algorithm: str = "ES256",
) -> str:
    """Mint a JWT for tests."""
    now = datetime.now(tz=UTC)
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
    }
    if include_exp:
        claims["exp"] = int((now + exp_delta).timestamp())
    if include_iat:
        claims["iat"] = int(now.timestamp())
    if include_jti:
        claims["jti"] = "jti-abc-123"
    return jwt.encode(claims, private_pem, algorithm=algorithm)


# ---------------------------------------------------------------------------
# JWTRegistrationValidator unit tests
# ---------------------------------------------------------------------------


class TestJWTRegistrationValidator:
    def test_valid_jwt_accepted(self, ec_keypair: tuple[str, str]) -> None:
        private_pem, public_pem = ec_keypair
        token = _mint_jwt(private_pem)
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )

        claims = validator.validate(f"Bearer {token}")

        assert claims.subject == "registrar-bot"
        assert claims.jti == "jti-abc-123"
        assert claims.issued_at.tzinfo is not None
        assert claims.raw["iss"] == ISSUER
        assert claims.raw["aud"] == AUDIENCE

    def test_wrong_issuer_rejected(self, ec_keypair: tuple[str, str]) -> None:
        private_pem, public_pem = ec_keypair
        token = _mint_jwt(private_pem, issuer="https://evil.test/oauth")
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        with pytest.raises(RegistrationAuthError):
            validator.validate(token)

    def test_wrong_audience_rejected(self, ec_keypair: tuple[str, str]) -> None:
        private_pem, public_pem = ec_keypair
        token = _mint_jwt(private_pem, audience="https://other.test/registry/servers")
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        with pytest.raises(RegistrationAuthError):
            validator.validate(token)

    def test_expired_token_rejected(self, ec_keypair: tuple[str, str]) -> None:
        private_pem, public_pem = ec_keypair
        # exp 30s in the past; leeway=0 so the expiry is enforced strictly.
        token = _mint_jwt(private_pem, exp_delta=timedelta(seconds=-30))
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
            leeway=timedelta(seconds=0),
        )
        with pytest.raises(RegistrationAuthError):
            validator.validate(token)

    def test_missing_exp_claim_rejected(self, ec_keypair: tuple[str, str]) -> None:
        """Tokens without an ``exp`` claim must be rejected.

        PyJWT's ``verify_exp`` default only *checks* ``exp`` when
        present — it does not require its presence.  The validator
        threads ``options={"require": ["exp"]}`` into ``jwt.decode`` so
        a token minted without an expiry cannot live forever.  This is
        a regression shield against accidentally dropping the
        ``require`` option: without it, the otherwise-valid token
        below would be accepted.
        """
        private_pem, public_pem = ec_keypair
        token = _mint_jwt(private_pem, include_exp=False)
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        with pytest.raises(RegistrationAuthError):
            validator.validate(token)

    def test_malformed_bearer_rejected(self, ec_keypair: tuple[str, str]) -> None:
        _, public_pem = ec_keypair
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        with pytest.raises(RegistrationAuthError):
            validator.validate("not-even-a-jwt")

    def test_missing_bearer_prefix_handled(self, ec_keypair: tuple[str, str]) -> None:
        """Validator accepts both ``Bearer <jwt>`` and a bare ``<jwt>``.

        The wire format the route handler sees is always
        ``Authorization: Bearer <jwt>`` — but accepting the bare form
        lets callers (including tests and in-process integrations)
        pass the token alone without re-prepending the scheme.
        ``bearer`` is also accepted case-insensitively per RFC 6750.
        """
        private_pem, public_pem = ec_keypair
        token = _mint_jwt(private_pem)
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        # Bare.
        assert validator.validate(token).subject == "registrar-bot"
        # Titlecase prefix.
        assert validator.validate(f"Bearer {token}").subject == "registrar-bot"
        # Lowercase prefix.
        assert validator.validate(f"bearer {token}").subject == "registrar-bot"

    def test_empty_bearer_rejected(self, ec_keypair: tuple[str, str]) -> None:
        _, public_pem = ec_keypair
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        for bearer in ("", "Bearer ", "   "):
            with pytest.raises(RegistrationAuthError):
                validator.validate(bearer)

    def test_rejects_none_algorithm_at_construction(self, ec_keypair: tuple[str, str]) -> None:
        """``algorithms=["none"]`` must raise at constructor time.

        Regression shield for the defense-in-depth check: the env-var
        parser already rejects "none", but a caller that constructs
        ``JWTRegistrationValidator`` programmatically with
        ``algorithms=["none"]`` would otherwise thread the literal
        through to ``jwt.decode``, which accepts unsigned tokens when
        "none" is in the allowed list. Failing at construction closes
        that bypass regardless of how the validator is wired.
        """
        _, public_pem = ec_keypair
        # Match just the token "none" case-insensitively rather than
        # the full error phrase — the test's job is to confirm the
        # constructor rejects the "none" algorithm, not to pin the
        # exact wording of the error message.
        with pytest.raises(ValueError, match=r"(?i)none"):
            JWTRegistrationValidator(
                public_key=public_pem,
                issuer=ISSUER,
                audience=AUDIENCE,
                algorithms=["none"],
            )

    def test_rejects_none_algorithm_case_insensitive(self, ec_keypair: tuple[str, str]) -> None:
        """Casing variants of 'none' are rejected too."""
        _, public_pem = ec_keypair
        for variant in ("None", "NONE", " none ", "NoNe"):
            with pytest.raises(ValueError, match=r"(?i)none"):
                JWTRegistrationValidator(
                    public_key=public_pem,
                    issuer=ISSUER,
                    audience=AUDIENCE,
                    algorithms=[variant],
                )

    def test_rejects_none_algorithm_in_mixed_list(self, ec_keypair: tuple[str, str]) -> None:
        """A list mixing valid algs with 'none' is still rejected.

        Catches the "sneak 'none' in among real algorithms" vector —
        PyJWT would still honor the 'none' entry and accept unsigned
        tokens.
        """
        _, public_pem = ec_keypair
        with pytest.raises(ValueError, match=r"(?i)none"):
            JWTRegistrationValidator(
                public_key=public_pem,
                issuer=ISSUER,
                audience=AUDIENCE,
                algorithms=["ES256", "none"],
            )

    def test_rejects_malformed_pem_at_construction(self) -> None:
        """A bogus ``public_key`` string must fail at constructor time.

        Without the parse-at-construction guard, a malformed PEM only
        surfaces when ``jwt.decode`` is first called — at which point
        the error is an opaque ``InvalidKeyError`` that the route
        layer collapses to a generic 401, hiding what is almost always
        a deployment misconfiguration.  The guard turns the runtime
        surprise into a startup failure the operator can act on.

        Match is loose (``r"(?i)pem"``) to avoid pinning the exact
        error phrasing while still confirming the rejection reason is
        about the PEM, not some other constructor-level validation.
        """
        with pytest.raises(ValueError, match=r"(?i)pem"):
            JWTRegistrationValidator(
                public_key="not-a-valid-pem-at-all",
                issuer=ISSUER,
                audience=AUDIENCE,
            )

    def test_iat_bool_true_falls_back_to_receive_time(self, ec_keypair: tuple[str, str]) -> None:
        """An ``iat`` claim that is a boolean must not coerce to epoch 1.

        ``bool`` is a subclass of ``int`` in Python, so without the
        explicit bool-reject the previous implementation would treat
        ``iat=true`` as ``iat=1`` and record 1970-01-01T00:00:01 in the
        audit trail. Fall through to the receive-time fallback instead.
        """
        private_pem, public_pem = ec_keypair
        # Mint a token with ``iat=True`` by bypassing _mint_jwt and
        # building the payload directly.
        now = datetime.now(tz=UTC)
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "registrar-bot",
            "iat": True,  # the adversarial value
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        }
        token = jwt.encode(payload, private_pem, algorithm="ES256")

        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        claims = validator.validate(token)
        # Fallback is the receive timestamp — within a few seconds of now,
        # not 1970-01-01T00:00:01.
        assert abs((claims.issued_at - now).total_seconds()) < 5

    def test_iat_extreme_values_do_not_crash_validator(
        self,
        ec_keypair: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Extreme / non-finite ``iat`` values fall back to receive time.

        PyJWT's current decode path rejects ``iat=inf`` / ``iat=nan`` /
        very-large-finite ``iat`` before our parsing runs, so the
        defense is belt-and-suspenders for a future PyJWT relaxation.
        Stub ``jwt.decode`` to return a payload directly so we exercise
        the validator's own parsing layer, confirming
        ``datetime.fromtimestamp`` failures are suppressed and the
        receive-time fallback is used.
        """
        _, public_pem = ec_keypair
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        now = datetime.now(tz=UTC)
        for bad_iat in (float("inf"), float("-inf"), float("nan"), 1e20, -1e20):
            stub_payload = {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "registrar-bot",
                "iat": bad_iat,
            }
            # ``jwt`` is imported locally inside ``validate``; patch the
            # module-level symbol so both imports resolve to the stub.
            monkeypatch.setattr(jwt, "decode", lambda *a, payload=stub_payload, **kw: payload)
            claims = validator.validate("stub-bearer")
            assert abs((claims.issued_at - now).total_seconds()) < 5, (
                f"iat={bad_iat!r} should fall back to receive time"
            )

    def test_signature_mismatch_rejected(self, ec_keypair: tuple[str, str]) -> None:
        """A JWT signed by a different key is rejected."""
        _, public_pem = ec_keypair
        # Mint with a *different* keypair so the signature does not
        # verify against ``public_pem``.
        other_private = ec.generate_private_key(ec.SECP256R1())
        other_private_pem = other_private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        token = _mint_jwt(other_private_pem)

        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        with pytest.raises(RegistrationAuthError):
            validator.validate(token)


# ---------------------------------------------------------------------------
# GatewayServer integration — full route path with validator
# ---------------------------------------------------------------------------


def _create_sales_server() -> FastMCP:
    mcp = FastMCP("sales-upstream")

    @mcp.tool()
    def sales_contacts_search(query: str) -> str:
        """Search sales contacts."""
        return json.dumps({"contacts": [{"name": "Alice"}]})

    return mcp


class TestGatewayIntegrationWithJWTValidator:
    @pytest.mark.asyncio
    async def test_valid_jwt_accepted_on_list_route(self, ec_keypair: tuple[str, str]) -> None:
        private_pem, public_pem = ec_keypair
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        gateway = GatewayServer(
            {"sales": _create_sales_server()},  # type: ignore[dict-item]
            registration_validator=validator,
        )
        await gateway.populate()

        token = _mint_jwt(private_pem)
        app = gateway.mcp.http_app(transport="streamable-http")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get(
                "/registry/servers",
                headers={"authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["servers"][0]["domain"] == "sales"

    @pytest.mark.asyncio
    async def test_invalid_jwt_returns_401(self, ec_keypair: tuple[str, str]) -> None:
        _, public_pem = ec_keypair
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        gateway = GatewayServer(
            {"sales": _create_sales_server()},  # type: ignore[dict-item]
            registration_validator=validator,
        )
        await gateway.populate()

        app = gateway.mcp.http_app(transport="streamable-http")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get(
                "/registry/servers",
                headers={"authorization": "Bearer not-a-valid-jwt"},
            )

        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_expired_jwt_returns_401(self, ec_keypair: tuple[str, str]) -> None:
        private_pem, public_pem = ec_keypair
        validator = JWTRegistrationValidator(
            public_key=public_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
            leeway=timedelta(seconds=0),
        )
        gateway = GatewayServer(
            {"sales": _create_sales_server()},  # type: ignore[dict-item]
            registration_validator=validator,
        )
        await gateway.populate()

        token = _mint_jwt(private_pem, exp_delta=timedelta(seconds=-30))
        app = gateway.mcp.http_app(transport="streamable-http")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get(
                "/registry/servers",
                headers={"authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 401
