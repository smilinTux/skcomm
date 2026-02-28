"""CapAuth token validator for WebRTC signaling authentication.

Validates CapAuth PGP-signed bearer tokens used to authenticate agents
on the WebSocket signaling endpoint. Returns the PGP fingerprint of the
authenticated agent, or None on failure.

In production, this can call the CapAuth verification API or validate
locally using the agent's PGP keyring. In development, the token can
be the raw 40-hex PGP fingerprint for quick testing.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("skcomm.capauth_validator")

# PGP fingerprint: 40 hex characters
_FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


class CapAuthValidator:
    """Validates CapAuth bearer tokens for WebRTC signaling authentication.

    Supports two validation modes:

    - **Local** (default): validates token format and extracts the embedded
      PGP fingerprint. Suitable for development and trusted networks where
      the signaling broker is not internet-exposed.
    - **Remote**: calls the CapAuth API endpoint to verify the token
      signature. Set ``capauth_url`` to enable full remote validation.

    Args:
        capauth_url: Optional CapAuth API base URL for remote validation
            (e.g. ``https://capauth.skworld.io``). If None, uses local mode.
        require_auth: If True, reject connections with no/invalid token.
            Set to False in development to allow unauthenticated peers (they
            get an "anonymous" pseudo-fingerprint).
    """

    def __init__(
        self,
        capauth_url: Optional[str] = None,
        require_auth: bool = True,
    ):
        self._capauth_url = capauth_url
        self._require_auth = require_auth

    def validate(self, token: Optional[str]) -> Optional[str]:
        """Validate a CapAuth bearer token and return the PGP fingerprint.

        Args:
            token: Raw token string from ``Authorization: Bearer <token>``.
                May be None if no Authorization header was provided.

        Returns:
            PGP fingerprint (40 uppercase hex chars) if valid.
            ``"anonymous"`` if ``require_auth`` is False and token is missing.
            None if validation fails and ``require_auth`` is True.
        """
        if not token:
            if self._require_auth:
                logger.warning("WebRTC signaling: no auth token — rejecting connection")
                return None
            return "anonymous"

        if self._capauth_url:
            return self._validate_remote(token)

        return self._validate_local(token)

    def _validate_local(self, token: str) -> Optional[str]:
        """Local validation: extract PGP fingerprint from token payload.

        Accepted token formats:
        - Plain 40-hex fingerprint: ``CCBE9306410CF8CD5E393D6DEC31663B95230684``
        - Fingerprint-prefixed: ``CCBE9306410CF8CD5E393D6DEC31663B95230684.<payload>``

        In production deployments, this method should verify a PGP signature
        over the token payload using the agent's CapAuth public key. For now
        it extracts the fingerprint portion and validates its format.

        Args:
            token: Bearer token string.

        Returns:
            Uppercase PGP fingerprint, or None if the token is invalid and
            ``require_auth`` is True.
        """
        # Handle "fingerprint.payload" format (e.g. JWT-like tokens)
        parts = token.split(".", 1)
        candidate = parts[0].upper()
        if _FINGERPRINT_RE.match(candidate):
            return candidate

        # Try the full token as a plain fingerprint (dev usage)
        if _FINGERPRINT_RE.match(token.upper()):
            return token.upper()

        logger.warning("WebRTC signaling: token does not contain a valid PGP fingerprint")
        if self._require_auth:
            return None

        # Permissive mode: derive a pseudo-fingerprint from the token for logging
        pseudo = (token[:40]).upper().ljust(40, "0")
        return pseudo

    def _validate_remote(self, token: str) -> Optional[str]:
        """Remote validation via CapAuth API.

        Calls ``POST {capauth_url}/api/v1/verify`` with the bearer token.
        The API should return ``{"fingerprint": "<40-hex>", "valid": true}``.

        Args:
            token: Bearer token string.

        Returns:
            Fingerprint from CapAuth response, or None on failure.
        """
        import json as _json
        import urllib.request

        try:
            req = urllib.request.Request(
                f"{self._capauth_url}/api/v1/verify",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
                fp = data.get("fingerprint")
                if fp and _FINGERPRINT_RE.match(str(fp).upper()):
                    return str(fp).upper()
                logger.warning("CapAuth response missing fingerprint: %s", data)
                return None
        except Exception as exc:
            logger.error("CapAuth remote validation failed: %s", exc)
            if self._require_auth:
                return None
            # Fallback to local validation if remote is unreachable
            return self._validate_local(token)
