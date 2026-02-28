"""Integration tests for the Nostr transport against live relays.

Tests relay connectivity, message encoding (NIP-44/NIP-59), event
publishing/querying, and full round-trip delivery via a public relay.

All tests are skipped when:
- Nostr crypto dependencies are not installed
- The target relay is unreachable (CI / offline environments)

Run with a live relay::

    pytest skcomm/tests/integration/test_nostr_relay.py -v

Force-enable (relay assumed reachable)::

    pytest skcomm/tests/integration/test_nostr_relay.py -v --no-relay-skip
"""

from __future__ import annotations

import base64
import json
import time
from typing import Optional

import pytest

from skcomm.transports.nostr import (
    DEFAULT_RELAYS,
    KIND_GIFT_WRAP,
    NOSTR_AVAILABLE,
    NostrTransport,
    _make_event,
    _pubkey_of,
    _query_relay,
    _publish_to_relay,
    _random_secret,
    _sign_event,
    nip44_conversation_key,
    nip44_decrypt,
    nip44_encrypt,
    unwrap_dm,
    wrap_dm,
)
from skcomm.transport import TransportStatus

# ---------------------------------------------------------------------------
# Test relay configuration
# ---------------------------------------------------------------------------

# nos.lol is a well-maintained public relay with fast responses.
# Damus is the fallback if nos.lol is unavailable.
CANDIDATE_RELAYS = ["wss://nos.lol", "wss://relay.damus.io", "wss://relay.nostr.band"]

# Generous timeout for CI/slow connections.
RELAY_CONNECT_TIMEOUT = 10.0
RELAY_OP_TIMEOUT = 12.0


# ---------------------------------------------------------------------------
# pytest plugin: --no-relay-skip flag
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    """Add --no-relay-skip to bypass relay-reachability check."""
    try:
        parser.addoption(
            "--no-relay-skip",
            action="store_true",
            default=False,
            help="Assume relay is reachable; skip connectivity pre-check.",
        )
    except ValueError:
        # Option already registered (e.g. multiple conftest.py files).
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_reach_relay(relay_url: str, timeout: float = RELAY_CONNECT_TIMEOUT) -> bool:
    """Return True if a WebSocket handshake to *relay_url* succeeds."""
    if not NOSTR_AVAILABLE:
        return False
    try:
        from websockets.sync.client import connect as _ws_connect

        with _ws_connect(relay_url, open_timeout=timeout, close_timeout=1):
            pass
        return True
    except Exception:
        return False


def _find_reachable_relay(timeout: float = RELAY_CONNECT_TIMEOUT) -> Optional[str]:
    """Return the first reachable relay from CANDIDATE_RELAYS, or None."""
    for url in CANDIDATE_RELAYS:
        if _try_reach_relay(url, timeout):
            return url
    return None


# ---------------------------------------------------------------------------
# Module-level skip when deps missing
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not NOSTR_AVAILABLE,
    reason="Nostr crypto deps not installed (pip install 'websockets>=12' cryptography)",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_relay(request) -> str:
    """Resolve a reachable relay URL or skip the entire module.

    Honours --no-relay-skip to assume connectivity (useful in local dev
    when you know the relay is up but the check is slow).
    """
    no_skip = request.config.getoption("--no-relay-skip", default=False)
    if no_skip:
        return CANDIDATE_RELAYS[0]

    relay = _find_reachable_relay()
    if relay is None:
        pytest.skip(
            f"No Nostr relay reachable from {CANDIDATE_RELAYS}. "
            "Skipping live integration tests. "
            "Pass --no-relay-skip to override."
        )
    return relay


@pytest.fixture
def sender_secret() -> bytes:
    return _random_secret()


@pytest.fixture
def receiver_secret() -> bytes:
    return _random_secret()


@pytest.fixture
def sender_pubkey(sender_secret: bytes) -> str:
    x, _ = _pubkey_of(sender_secret)
    return x.hex()


@pytest.fixture
def receiver_pubkey(receiver_secret: bytes) -> str:
    x, _ = _pubkey_of(receiver_secret)
    return x.hex()


@pytest.fixture
def sample_envelope_bytes() -> bytes:
    """Minimal SKComm envelope with a unique ID per test run."""
    return json.dumps({
        "skcomm_version": "1.0.0",
        "envelope_id": f"integ-{int(time.time() * 1000)}",
        "sender": "jarvis",
        "recipient": "lumina",
        "payload": {
            "content": "Nostr integration test — staycurious",
            "content_type": "text",
        },
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. Relay connectivity
# ---------------------------------------------------------------------------


class TestRelayConnectivity:
    """Verify that the chosen relay is reachable and speaks NIP-01."""

    def test_websocket_handshake(self, live_relay: str):
        """A plain WebSocket connection opens and closes cleanly."""
        assert _try_reach_relay(live_relay, timeout=RELAY_CONNECT_TIMEOUT)

    def test_health_check_reports_available(self, live_relay: str):
        """NostrTransport.health_check() reports at least one relay reachable.

        Uses all candidate relays so the check is robust to any single relay
        briefly dropping the connection between tests.
        """
        # Include all candidates so transient flakiness on one doesn't fail the test.
        relays_to_check = list(dict.fromkeys([live_relay] + CANDIDATE_RELAYS))
        t = NostrTransport(relays=relays_to_check, relay_timeout=RELAY_CONNECT_TIMEOUT)
        health = t.health_check()
        assert health.status in (TransportStatus.AVAILABLE, TransportStatus.DEGRADED), (
            f"Expected AVAILABLE or DEGRADED, got {health.status}: {health.error}"
        )
        assert health.details["reachable_relays"] >= 1

    def test_health_check_latency_measured(self, live_relay: str):
        """Health check records a positive latency."""
        t = NostrTransport(relays=[live_relay], relay_timeout=RELAY_CONNECT_TIMEOUT)
        health = t.health_check()
        assert health.latency_ms is not None
        assert health.latency_ms > 0

    def test_eose_received_on_empty_query(self, live_relay: str):
        """Querying with a very recent 'since' returns EOSE quickly (no events expected)."""
        # Use a future timestamp so there are definitely no matching events.
        filters = {
            "kinds": [KIND_GIFT_WRAP],
            "#p": ["0" * 64],
            "since": int(time.time()) + 3600,
        }
        events = _query_relay(live_relay, filters, timeout=RELAY_OP_TIMEOUT)
        # We don't assert on count — relay may return 0 or more — but no exception.
        assert isinstance(events, list)


# ---------------------------------------------------------------------------
# 2. Message encoding (no network needed, but grouped here for completeness)
# ---------------------------------------------------------------------------


class TestMessageEncoding:
    """Verify the encoding pipeline used before hitting the wire."""

    def test_envelope_base64_roundtrip(self, sample_envelope_bytes: bytes):
        """Envelope bytes survive base64 encode → decode unchanged."""
        encoded = base64.b64encode(sample_envelope_bytes).decode()
        decoded = base64.b64decode(encoded)
        assert decoded == sample_envelope_bytes

    def test_nip44_encrypt_decrypt(self, sender_secret: bytes, receiver_secret: bytes, receiver_pubkey: str):
        """NIP-44 encryption is symmetric and reversible."""
        conv_key = nip44_conversation_key(sender_secret, bytes.fromhex(receiver_pubkey))
        plaintext = base64.b64encode(b"sovereign agent payload").decode()
        ciphertext = nip44_encrypt(conv_key, plaintext)
        assert plaintext != ciphertext
        recovered = nip44_decrypt(conv_key, ciphertext)
        assert recovered == plaintext

    def test_gift_wrap_structure(
        self,
        sender_secret: bytes,
        sender_pubkey: str,
        receiver_pubkey: str,
    ):
        """wrap_dm produces a well-formed kind-1059 event."""
        gift = wrap_dm(sender_secret, sender_pubkey, receiver_pubkey, "hello relay")
        assert gift["kind"] == KIND_GIFT_WRAP
        assert "id" in gift
        assert "sig" in gift
        assert len(gift["sig"]) == 128
        p_tags = [t for t in gift["tags"] if t[0] == "p"]
        assert len(p_tags) == 1
        assert p_tags[0][1] == receiver_pubkey

    def test_gift_wrap_unwrap_roundtrip(
        self,
        sender_secret: bytes,
        sender_pubkey: str,
        receiver_secret: bytes,
        receiver_pubkey: str,
    ):
        """wrap_dm then unwrap_dm recovers the original content and sender."""
        content = "skcomm nostr roundtrip content"
        gift = wrap_dm(sender_secret, sender_pubkey, receiver_pubkey, content)
        result = unwrap_dm(receiver_secret, gift)
        assert result is not None, "unwrap_dm returned None — decryption failed"
        recovered_pubkey, recovered_content = result
        assert recovered_content == content
        assert recovered_pubkey == sender_pubkey

    def test_gift_wrap_uses_ephemeral_pubkey(
        self,
        sender_secret: bytes,
        sender_pubkey: str,
        receiver_pubkey: str,
    ):
        """Outer gift-wrap pubkey must differ from the real sender pubkey (NIP-59)."""
        gift = wrap_dm(sender_secret, sender_pubkey, receiver_pubkey, "test")
        assert gift["pubkey"] != sender_pubkey, (
            "Gift-wrap pubkey matches sender — metadata is leaking!"
        )

    def test_event_id_is_sha256_of_canonical_form(
        self,
        sender_secret: bytes,
        sender_pubkey: str,
    ):
        """Signed event IDs follow NIP-01: SHA-256 of the canonical JSON array."""
        import hashlib

        event = _make_event(sender_pubkey, 1, "test content", [])
        _sign_event(event, sender_secret)

        canonical = json.dumps(
            [
                0,
                event["pubkey"],
                event["created_at"],
                event["kind"],
                event["tags"],
                event["content"],
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        expected_id = hashlib.sha256(canonical.encode()).hexdigest()
        assert event["id"] == expected_id


# ---------------------------------------------------------------------------
# 3. Live publish → query
# ---------------------------------------------------------------------------


class TestLivePublishQuery:
    """Publish a real Nostr event and retrieve it from the relay."""

    def test_publish_kind1_event(self, live_relay: str, sender_secret: bytes, sender_pubkey: str):
        """A signed kind-1 note can be published to the relay."""
        event = _make_event(sender_pubkey, 1, "SKComm integration test note", [])
        _sign_event(event, sender_secret)
        accepted = _publish_to_relay(live_relay, event, timeout=RELAY_OP_TIMEOUT)
        assert accepted, f"Relay {live_relay} rejected the event (OK false or no response)"

    def test_publish_gift_wrap_and_query(
        self,
        live_relay: str,
        sender_secret: bytes,
        sender_pubkey: str,
        receiver_secret: bytes,
        receiver_pubkey: str,
    ):
        """Publish a gift-wrapped DM, then verify it is stored on the relay.

        Queries by event ID (NIP-01 ``ids`` filter) rather than the ``#p``
        tag filter, because many public relays require NIP-42 authentication
        before returning kind-1059 events via tag-based queries.
        """
        content = f"integration-{int(time.time())}"
        gift = wrap_dm(sender_secret, sender_pubkey, receiver_pubkey, content)

        published = _publish_to_relay(live_relay, gift, timeout=RELAY_OP_TIMEOUT)
        assert published, f"Relay {live_relay} rejected gift-wrap event"

        # Brief propagation window before querying.
        time.sleep(1.0)

        # Query by event ID — this works without NIP-42 auth on all NIP-01
        # compliant relays, regardless of event kind.
        filters = {"ids": [gift["id"]]}
        events = _query_relay(live_relay, filters, timeout=RELAY_OP_TIMEOUT)

        assert len(events) == 1, (
            f"Published event id={gift['id'][:8]}… not found by ID query on {live_relay}. "
            f"Got {len(events)} events total."
        )
        assert events[0]["id"] == gift["id"]
        assert events[0]["kind"] == KIND_GIFT_WRAP

    def test_query_retrieves_correct_event_content(
        self,
        live_relay: str,
        sender_secret: bytes,
        sender_pubkey: str,
        receiver_secret: bytes,
        receiver_pubkey: str,
    ):
        """Events retrieved from the relay by ID decrypt to the original plaintext."""
        original = f"sovereign-payload-{int(time.time())}"
        gift = wrap_dm(sender_secret, sender_pubkey, receiver_pubkey, original)

        published = _publish_to_relay(live_relay, gift, timeout=RELAY_OP_TIMEOUT)
        assert published, f"Relay {live_relay} rejected gift-wrap event"

        time.sleep(1.0)

        # Retrieve by event ID so we don't depend on NIP-42 auth for #p filters.
        events = _query_relay(live_relay, {"ids": [gift["id"]]}, timeout=RELAY_OP_TIMEOUT)
        assert events, f"Published event id={gift['id'][:8]}… not found on {live_relay}"

        result = unwrap_dm(receiver_secret, events[0])
        assert result is not None, "unwrap_dm failed on event retrieved from relay"
        _, recovered = result
        assert recovered == original


# ---------------------------------------------------------------------------
# 4. Full round-trip via NostrTransport
# ---------------------------------------------------------------------------


class TestNostrTransportRoundTrip:
    """End-to-end: NostrTransport.send() → relay → NostrTransport.receive()."""

    def test_send_succeeds_via_live_relay(
        self,
        live_relay: str,
        sample_envelope_bytes: bytes,
        receiver_secret: bytes,
        receiver_pubkey: str,
    ):
        """NostrTransport.send() returns success when a relay accepts the event."""
        sender_t = NostrTransport(relays=[live_relay], relay_timeout=RELAY_OP_TIMEOUT)
        result = sender_t.send(sample_envelope_bytes, receiver_pubkey)
        assert result.success, (
            f"send() failed: {result.error}. "
            f"Transport={result.transport_name}, relay={live_relay}"
        )
        assert result.transport_name == "nostr"
        assert result.latency_ms is not None and result.latency_ms > 0

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Public Nostr relays commonly require NIP-42 authentication before "
            "returning kind-1059 events via #p tag filters. "
            "This test passes on relays that support unauthenticated #p queries "
            "(e.g. self-hosted relays or relays with open policies)."
        ),
    )
    def test_receive_recovers_sent_envelope(
        self,
        live_relay: str,
        sample_envelope_bytes: bytes,
        receiver_secret: bytes,
        receiver_pubkey: str,
    ):
        """Envelope sent by sender_t is recovered via receiver_t.receive().

        May xfail on public relays that enforce NIP-42 for kind-1059 #p queries.
        """
        sender_t = NostrTransport(relays=[live_relay], relay_timeout=RELAY_OP_TIMEOUT)
        receiver_t = NostrTransport(
            private_key_hex=receiver_secret.hex(),
            relays=[live_relay],
            relay_timeout=RELAY_OP_TIMEOUT,
            since_window=300,  # Look back 5 minutes.
        )

        send_result = sender_t.send(sample_envelope_bytes, receiver_pubkey)
        assert send_result.success, f"send() failed: {send_result.error}"

        time.sleep(1.5)  # Propagation window.

        received = receiver_t.receive()
        assert len(received) >= 1, (
            "receiver_t.receive() returned no messages after a successful send. "
            f"Relay: {live_relay}"
        )

        # Find our specific envelope by ID.
        sent_id = json.loads(sample_envelope_bytes)["envelope_id"]
        matched = [
            m for m in received
            if json.loads(m).get("envelope_id") == sent_id
        ]
        assert len(matched) == 1, (
            f"Sent envelope_id={sent_id!r} not found among {len(received)} received messages."
        )

        recovered = json.loads(matched[0])
        assert recovered["sender"] == "jarvis"
        assert recovered["recipient"] == "lumina"
        assert recovered["payload"]["content_type"] == "text"

    @pytest.mark.xfail(
        strict=False,
        reason="Depends on relay returning kind-1059 events without NIP-42 auth.",
    )
    def test_receive_deduplication_on_live_relay(
        self,
        live_relay: str,
        sample_envelope_bytes: bytes,
        receiver_secret: bytes,
        receiver_pubkey: str,
    ):
        """Calling receive() twice does not double-deliver the same event.

        May xfail on public relays that enforce NIP-42 for kind-1059 #p queries.
        """
        sender_t = NostrTransport(relays=[live_relay], relay_timeout=RELAY_OP_TIMEOUT)
        receiver_t = NostrTransport(
            private_key_hex=receiver_secret.hex(),
            relays=[live_relay],
            relay_timeout=RELAY_OP_TIMEOUT,
            since_window=300,
        )

        send_result = sender_t.send(sample_envelope_bytes, receiver_pubkey)
        assert send_result.success

        time.sleep(1.5)

        first_batch = receiver_t.receive()
        second_batch = receiver_t.receive()

        sent_id = json.loads(sample_envelope_bytes)["envelope_id"]
        first_ids = {json.loads(m).get("envelope_id") for m in first_batch}
        second_ids = {json.loads(m).get("envelope_id") for m in second_batch}

        assert sent_id in first_ids, "Envelope not received in first batch"
        assert sent_id not in second_ids, (
            "Envelope was delivered twice — deduplication is broken"
        )

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Public relays may rate-limit publishing after multiple events in the "
            "same test session, causing all fallback relays to temporarily reject. "
            "Run this test in isolation (pytest test_nostr_relay.py::... -k fallback) "
            "to verify fallback behaviour without rate-limit interference."
        ),
    )
    def test_multi_relay_fallback(
        self,
        live_relay: str,
        sample_envelope_bytes: bytes,
        receiver_secret: bytes,
        receiver_pubkey: str,
    ):
        """Transport falls through to a live relay when the first relay refuses.

        Uses ws://127.0.0.1:1 as the "dead" relay — connection refused is
        returned in milliseconds so we don't burn the full relay_timeout.
        The transport must then fall through to a live relay and succeed.

        Note: May xfail in a full suite run when relays rate-limit after
        multiple prior publishes.  Passes consistently when run in isolation.
        """
        # Port 1 on loopback: privileged, not listening → instant refusal.
        dead_relay = "ws://127.0.0.1:1"
        # Include all candidates so at least one live relay is available.
        fallback_relays = list(dict.fromkeys([live_relay] + CANDIDATE_RELAYS))
        sender_t = NostrTransport(
            relays=[dead_relay] + fallback_relays,
            relay_timeout=RELAY_OP_TIMEOUT,
        )
        result = sender_t.send(sample_envelope_bytes, receiver_pubkey)
        # Should succeed via a live relay after the dead one fails fast.
        assert result.success, (
            f"Expected fallback after {dead_relay} failed, "
            f"but all relays rejected: {result.error}"
        )
