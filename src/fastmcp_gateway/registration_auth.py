"""Registration endpoint authentication primitives.

The ``/registry/servers`` REST routes originally authenticated callers
with a single shared static bearer — any process that had the token
could register upstreams, and there was no per-caller identity, no
rotation hook, and no audit of who registered what.  A leaked token
meant unattributable, permanent access.

This module adds a validator abstraction + a concrete JWT-based
implementation so deployments can replace the static bearer with a
short-lived, signed, per-caller token.

Design points:

* :class:`RegistrationTokenValidator` is a :class:`Protocol` (not an
  abstract base class) so callers can supply any object with a
  ``validate(bearer) -> RegistrationClaims`` method — including test
  fakes, in-process HMAC validators, or alternative JWT libraries —
  without having to inherit from a concrete type.
* :class:`JWTRegistrationValidator` performs a standard signed-JWT
  verification with strict issuer + audience + expiry checks.  No
  single-use ``jti`` cache is implemented — short expiry (recommended
  ≤ 5 minutes at the issuer) is the primary replay mitigation in this
  release.  A cache can be layered on later without changing the
  public surface.
* The ``import jwt`` call is deferred to
  :meth:`JWTRegistrationValidator.__init__` so that the
  ``PyJWT[crypto]`` transitive cost (~7MB ``cryptography`` wheel) is
  paid only when a JWT validator is actually constructed.  Callers
  that stay on the static-bearer path — or disable registration
  entirely — don't pay the import cost.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


class RegistrationAuthError(ValueError):
    """Raised when a registration bearer cannot be validated.

    Subclasses :class:`ValueError` so existing exception handlers that
    catch the broad type still catch this.  The route layer in
    :mod:`fastmcp_gateway.gateway` catches this specific type and
    converts it to a 401 response.
    """


@dataclass(frozen=True)
class RegistrationClaims:
    """The subset of a validated bearer's claims that the gateway logs.

    * ``subject`` — the ``sub`` claim; typically the calling service /
      operator identity.  Used as the principal in the audit log line.
    * ``jti`` — the ``jti`` claim if present; ``None`` otherwise.
      Included for log correlation with the issuer's audit trail.
    * ``issued_at`` — timezone-aware UTC ``datetime`` derived from the
      ``iat`` claim.  Lets the audit log show the originating timestamp
      rather than the gateway's receive time, which is useful when
      diagnosing clock skew across the issuer / gateway boundary.
    * ``raw`` — the full decoded payload, kept so advanced integrations
      (e.g. a downstream policy check) can read custom claims without
      redecoding the bearer.
    """

    subject: str
    jti: str | None
    issued_at: datetime
    raw: dict[str, Any]


class RegistrationTokenValidator(Protocol):
    """Protocol implemented by registration bearer validators.

    A validator's single responsibility is to turn an
    ``Authorization`` header value (or the bearer alone) into a
    :class:`RegistrationClaims` instance — or raise
    :class:`RegistrationAuthError` if the bearer is invalid.  The route
    layer is the only caller and always wraps the call in an
    exception handler that converts the error to a 401 response.
    """

    def validate(self, bearer: str) -> RegistrationClaims: ...


class JWTRegistrationValidator:
    """Validates a registration bearer as a short-lived signed JWT.

    Parameters
    ----------
    public_key:
        PEM-encoded verification key.  Both EC (for ``ES256`` / ``ES384``
        / ``ES512``) and RSA (for ``RS256`` / ``RS384`` / ``RS512``) keys
        are accepted — the underlying ``jwt.decode`` call handles both
        shapes.
    issuer:
        Required ``iss`` claim value.  Any token whose ``iss`` does not
        exactly match is rejected.  Typically the URL of the operator's
        controller / sidecar component that mints short-lived tokens.
    audience:
        Required ``aud`` claim value.  Typically the gateway's
        registration endpoint URL (e.g.
        ``https://gateway.example/registry/servers``).  Ensures a token
        minted for a different gateway instance cannot be replayed at
        this one.
    algorithms:
        Allowed signing algorithms.  Defaults to ``["ES256"]`` — EC
        P-256 with SHA-256, a good default for short-lived tokens.
        Callers that mint with a different algorithm (e.g.
        ``["RS256"]``) must pass the matching list here.  ``"none"`` is
        not in the default list and must not be added — PyJWT accepts
        ``"none"`` only when explicitly listed, and allowing it would
        trivially bypass signature verification.
    leeway:
        Clock-skew tolerance applied to the ``exp`` / ``nbf`` checks.
        Default 10 seconds — large enough to absorb normal NTP drift
        between the issuer and the gateway, small enough that it does
        not meaningfully extend the replay window.

    Notes
    -----
    * ``exp`` is **required and enforced** — ``jwt.decode`` is invoked
      with ``options={"require": ["exp"]}`` so that a token minted
      without an expiry claim is rejected rather than silently accepted
      as permanent.  ``iat`` is read (for the audit log) but not
      enforced as a floor — a small negative ``iat`` relative to
      receive time can happen legitimately under clock skew, and the
      ``exp`` check already bounds usable lifetime from above.
    * No ``jti`` single-use cache.  Short expiry (recommended ≤ 5 min)
      is the primary replay mitigation.  A caller that needs strict
      single-use semantics can layer a cache on top of this validator
      by wrapping :meth:`validate`.
    """

    def __init__(
        self,
        *,
        public_key: str,
        issuer: str,
        audience: str,
        algorithms: list[str] | None = None,
        leeway: timedelta = timedelta(seconds=10),
    ) -> None:
        # Imports deferred here so that consumers that only use the
        # static-bearer path never pay the ``cryptography`` wheel
        # import cost.  The module-level surface is intentionally
        # jwt-free.  ``cryptography`` is already an install-time
        # dependency via ``PyJWT[crypto]``, so importing
        # ``load_pem_public_key`` adds no new transitive surface.
        import jwt  # noqa: F401  (imported for side-effects of install check)
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        if not public_key or not public_key.strip():
            raise ValueError("public_key must be a non-empty PEM-encoded key")
        # Parse the PEM up front.  Without this, a malformed key only
        # surfaces on the first ``validate()`` call — at which point
        # ``jwt.decode`` raises an opaque ``InvalidKeyError`` that the
        # route layer collapses to a generic 401, hiding what is almost
        # always a deployment misconfiguration.  Failing at construction
        # turns that runtime surprise into a startup error operators
        # can debug from CLI logs before any traffic arrives.  The
        # parsed key object is discarded — PyJWT accepts the PEM string
        # directly and re-parsing per-call is negligible, so keeping
        # the stored representation as ``str`` preserves the existing
        # ``validate()`` contract.
        try:
            load_pem_public_key(public_key.encode("utf-8"))
        except (ValueError, UnsupportedAlgorithm) as exc:
            raise ValueError("public_key must be a valid PEM-encoded public key") from exc
        if not issuer:
            raise ValueError("issuer must be a non-empty string")
        if not audience:
            raise ValueError("audience must be a non-empty string")

        self._public_key = public_key
        self._issuer = issuer
        self._audience = audience
        # Copy to avoid later external mutation of the list silently
        # changing the set of accepted algorithms on the live
        # validator.
        resolved_algorithms = list(algorithms) if algorithms else ["ES256"]
        # Defense-in-depth: the env-var parser in ``__main__.py``
        # already rejects ``none``, but constructing the validator
        # programmatically with ``algorithms=["none"]`` would otherwise
        # pass that literal straight through to ``jwt.decode``, which
        # would accept unsigned tokens. Reject at construction too so
        # neither path can open the bypass. Case-insensitive because
        # PyJWT accepts any casing (``"None"``, ``"NONE"``, etc.).
        for alg in resolved_algorithms:
            if isinstance(alg, str) and alg.strip().lower() == "none":
                raise ValueError(
                    "algorithms must not contain 'none' — unsigned JWTs would bypass signature verification"
                )
        self._algorithms = resolved_algorithms
        self._leeway = leeway

    def validate(self, bearer: str) -> RegistrationClaims:
        """Validate *bearer* and return its claims.

        Accepts both ``"Bearer <jwt>"`` and a bare ``"<jwt>"`` — the
        former is the wire form that reaches the route handler
        verbatim; stripping the prefix here lets the route layer pass
        the raw header value without per-route string surgery.
        """
        import jwt
        from jwt import (
            DecodeError,
            ExpiredSignatureError,
            InvalidAudienceError,
            InvalidIssuerError,
            InvalidTokenError,
        )

        if bearer is None:
            raise RegistrationAuthError("missing bearer")

        token = bearer.strip()
        # Case-insensitive ``Bearer `` strip — RFC 6750 is
        # case-insensitive on the scheme name, and some clients send
        # lowercase ``bearer``.
        if token.lower().startswith("bearer "):
            token = token[len("bearer ") :].strip()
        if not token:
            raise RegistrationAuthError("missing bearer")

        try:
            payload = jwt.decode(
                token,
                self._public_key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                # ``verify_exp`` defaults to True but only checks ``exp``
                # when present — require its presence explicitly so a
                # token minted without an expiry cannot be replayed
                # indefinitely. Missing-claim failures raise
                # ``MissingRequiredClaimError`` (an ``InvalidTokenError``
                # subtype) and are caught by the catch-all below, which
                # collapses to the uniform "invalid token" rejection.
                options={"require": ["exp"]},
            )
        except ExpiredSignatureError as exc:
            raise RegistrationAuthError("token expired") from exc
        except InvalidIssuerError as exc:
            raise RegistrationAuthError("issuer mismatch") from exc
        except InvalidAudienceError as exc:
            raise RegistrationAuthError("audience mismatch") from exc
        except DecodeError as exc:
            # Covers malformed structure, signature mismatch, etc. —
            # collapse to a single rejection reason at the route layer
            # so we don't advertise why validation failed to an
            # unauthenticated caller.
            raise RegistrationAuthError("invalid token") from exc
        except InvalidTokenError as exc:
            # Defensive catch-all for any other PyJWT validation
            # subtype (e.g. ImmatureSignatureError on ``nbf``); keeps
            # the public error surface uniform.
            raise RegistrationAuthError("invalid token") from exc

        # PyJWT guarantees the payload is a dict at this point; the
        # narrow ``isinstance`` guard is belt-and-suspenders against a
        # future change in PyJWT's return shape.
        if not isinstance(payload, dict):
            raise RegistrationAuthError("invalid token payload")

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise RegistrationAuthError("missing or invalid 'sub' claim")

        jti_raw = payload.get("jti")
        jti: str | None = jti_raw if isinstance(jti_raw, str) and jti_raw else None

        # ``iat`` is optional in the JWT spec and advisory-only here
        # (``exp`` is what actually bounds usable lifetime).  The
        # fallback for any missing, non-numeric, or malicious value is
        # the gateway-side receive timestamp so the audit log always
        # has *some* time reference.  Defensive parsing:
        #   * reject ``bool`` explicitly (``bool`` is a subclass of
        #     ``int`` so ``isinstance(True, int)`` is ``True`` — a
        #     literal ``true`` in the payload would otherwise coerce
        #     to epoch 1);
        #   * reject non-finite floats (``NaN`` / ``inf`` — Python's
        #     ``json`` parser allows these in non-strict mode, and
        #     ``fromtimestamp(inf)`` raises ``OverflowError``);
        #   * defend against extreme but finite values (e.g. ``1e20``)
        #     via ``try/except`` so an attacker-controlled JWT cannot
        #     crash the validator with an out-of-range timestamp.
        iat_raw = payload.get("iat")
        issued_at = datetime.now(tz=UTC)
        if not isinstance(iat_raw, bool) and isinstance(iat_raw, int | float):
            iat_float = float(iat_raw)
            if math.isfinite(iat_float):
                # Extreme-but-finite timestamps (e.g. ``1e20``) raise
                # ``OverflowError`` / ``OSError`` / ``ValueError`` on
                # different platforms; suppress and keep the
                # receive-time fallback so an attacker-controlled
                # payload cannot crash the validator.
                with contextlib.suppress(OverflowError, OSError, ValueError):
                    issued_at = datetime.fromtimestamp(iat_float, tz=UTC)

        return RegistrationClaims(
            subject=subject,
            jti=jti,
            issued_at=issued_at,
            raw=payload,
        )
