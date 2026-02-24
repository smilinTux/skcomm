"""Tests for the Nostr transport layer.

Tests crypto primitives (NIP-44, BIP-340, NIP-59), key management,
send/receive flows, and health checks. Relay I/O is mocked to avoid
network dependencies in CI.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from skcomm.transport import TransportCategory, TransportStatus
from skcomm.transports.nostr import (
    DEFAULT_RELAYS,
    KIND_DM,
    KIND_GIFT_WRAP,
    KIND_SEAL,
    NOSTR_AVAILABLE,
    NostrTransport,
    _make_event,
    _pubkey_of,
    _random_secret,
    _sign_event,
    create_transport,
    nip44_conversation_key,
    nip44_decrypt,
    nip44_encrypt,
    unwrap_dm,
    wrap_dm,
)


pytestmark = pytest.mark.skipif(
    not NOSTR_AVAILABLE,
    reason="Nostr crypto deps not installed (websockets, cryptography)",
)


@pytest.fixture
def sender_secret() -> bytes:
    """A deterministic sender secret key for testing."""
    return _random_secret()


@pytest.fixture
def recipient_secret() -> bytes:
    """A deterministic recipient secret key for testing."""
    return _random_secret()


@pytest.fixture
def sample_envelope() -> bytes:
    """A minimal SKComm envelope as bytes."""
    return json.dumps({
        "skcomm_version": "1.0.0",
        "envelope_id": "test-envelope-123",
        "sender": "jarvis",
        "recipient": "lumina",
        "payload": {"content": "Hello from Nostr!", "content_type": "text"},
    }).encode("utf-8")


# ═══════════════════════════════════════════════════════════
# BIP-340 / key helpers
# ═══════════════════════════════════════════════════════════


class TestKeyHelpers:
    """Test key generation and BIP-340 primitives."""

    def test_random_secret_valid(self):
        """Generated secrets are 32 bytes and non-zero."""
        secret = _random_secret()
        assert len(secret) == 32
        assert int.from_bytes(secret, "big") != 0

    def test_pubkey_derivation(self, sender_secret: bytes):
        """_pubkey_of returns 32-byte x-coordinate and parity flag."""
        x, even = _pubkey_of(sender_secret)
        assert len(x) == 32
        assert isinstance(even, bool)

    def test_different_secrets_different_pubkeys(self):
        """Two different secrets produce different public keys."""
        s1 = _random_secret()
        s2 = _random_secret()
        x1, _ = _pubkey_of(s1)
        x2, _ = _pubkey_of(s2)
        assert x1 != x2

    def test_sign_event_produces_valid_sig(self, sender_secret: bytes):
        """_sign_event populates sig field with 128-char hex."""
        x, _ = _pubkey_of(sender_secret)
        event = _make_event(x.hex(), 1, "test", [])
        _sign_event(event, sender_secret)
        assert len(event["sig"]) == 128


# ═══════════════════════════════════════════════════════════
# NIP-44 v2 encryption
# ═══════════════════════════════════════════════════════════


class TestNIP44:
    """Test NIP-44 v2 encrypt/decrypt cycle."""

    def test_encrypt_decrypt_roundtrip(self, sender_secret: bytes, recipient_secret: bytes):
        """Encrypt then decrypt recovers the original plaintext."""
        rx, _ = _pubkey_of(recipient_secret)
        conv_key = nip44_conversation_key(sender_secret, rx)
        plaintext = "staycuriousANDkeepsmilin"
        encrypted = nip44_encrypt(conv_key, plaintext)
        decrypted = nip44_decrypt(conv_key, encrypted)
        assert decrypted == plaintext

    def test_symmetric_conversation_key(self, sender_secret: bytes, recipient_secret: bytes):
        """Both parties derive the same conversation key."""
        sx, _ = _pubkey_of(sender_secret)
        rx, _ = _pubkey_of(recipient_secret)
        key_a = nip44_conversation_key(sender_secret, rx)
        key_b = nip44_conversation_key(recipient_secret, sx)
        assert key_a == key_b

    def test_encrypt_produces_base64(self, sender_secret: bytes, recipient_secret: bytes):
        """Encrypted output is valid base64."""
        rx, _ = _pubkey_of(recipient_secret)
        conv_key = nip44_conversation_key(sender_secret, rx)
        encrypted = nip44_encrypt(conv_key, "test")
        decoded = base64.b64decode(encrypted)
        assert decoded[0] == 0x02

    def test_decrypt_wrong_key_fails(self, sender_secret: bytes, recipient_secret: bytes):
        """Decryption with wrong key raises ValueError."""
        rx, _ = _pubkey_of(recipient_secret)
        conv_key = nip44_conversation_key(sender_secret, rx)
        encrypted = nip44_encrypt(conv_key, "secret message")
        wrong_key = _random_secret()
        with pytest.raises(ValueError, match="HMAC"):
            nip44_decrypt(wrong_key, encrypted)

    def test_encrypt_long_message(self, sender_secret: bytes, recipient_secret: bytes):
        """Encryption works for messages up to 64KB."""
        rx, _ = _pubkey_of(recipient_secret)
        conv_key = nip44_conversation_key(sender_secret, rx)
        long_msg = "A" * 60000
        encrypted = nip44_encrypt(conv_key, long_msg)
        decrypted = nip44_decrypt(conv_key, encrypted)
        assert decrypted == long_msg


# ═══════════════════════════════════════════════════════════
# NIP-17 / NIP-59 wrapping
# ═══════════════════════════════════════════════════════════


class TestGiftWrap:
    """Test NIP-59 gift wrap creation and unwrapping."""

    def test_wrap_unwrap_roundtrip(self, sender_secret: bytes, recipient_secret: bytes):
        """wrap_dm then unwrap_dm recovers the original content."""
        sx, _ = _pubkey_of(sender_secret)
        rx, _ = _pubkey_of(recipient_secret)
        gift = wrap_dm(sender_secret, sx.hex(), rx.hex(), "Hello Pengu Nation!")
        result = unwrap_dm(recipient_secret, gift)
        assert result is not None
        sender_pub, content = result
        assert content == "Hello Pengu Nation!"
        assert sender_pub == sx.hex()

    def test_gift_wrap_is_kind_1059(self, sender_secret: bytes, recipient_secret: bytes):
        """Gift wrap event has kind 1059."""
        sx, _ = _pubkey_of(sender_secret)
        rx, _ = _pubkey_of(recipient_secret)
        gift = wrap_dm(sender_secret, sx.hex(), rx.hex(), "test")
        assert gift["kind"] == KIND_GIFT_WRAP

    def test_gift_wrap_has_p_tag(self, sender_secret: bytes, recipient_secret: bytes):
        """Gift wrap event tags the recipient."""
        sx, _ = _pubkey_of(sender_secret)
        rx, _ = _pubkey_of(recipient_secret)
        gift = wrap_dm(sender_secret, sx.hex(), rx.hex(), "test")
        p_tags = [t for t in gift["tags"] if t[0] == "p"]
        assert len(p_tags) == 1
        assert p_tags[0][1] == rx.hex()

    def test_gift_wrap_ephemeral_pubkey(self, sender_secret: bytes, recipient_secret: bytes):
        """Gift wrap pubkey is ephemeral (different from sender)."""
        sx, _ = _pubkey_of(sender_secret)
        rx, _ = _pubkey_of(recipient_secret)
        gift = wrap_dm(sender_secret, sx.hex(), rx.hex(), "test")
        assert gift["pubkey"] != sx.hex()

    def test_unwrap_with_wrong_key_returns_none(self, sender_secret: bytes, recipient_secret: bytes):
        """Unwrapping with wrong key returns None."""
        sx, _ = _pubkey_of(sender_secret)
        rx, _ = _pubkey_of(recipient_secret)
        gift = wrap_dm(sender_secret, sx.hex(), rx.hex(), "secret")
        wrong = _random_secret()
        assert unwrap_dm(wrong, gift) is None


# ═══════════════════════════════════════════════════════════
# NostrTransport class
# ═══════════════════════════════════════════════════════════


class TestNostrTransport:
    """Test the NostrTransport class."""

    def test_create_transport_factory(self):
        """create_transport returns a configured NostrTransport."""
        t = create_transport(priority=5)
        assert t.name == "nostr"
        assert t.priority == 5
        assert t.category == TransportCategory.STEALTH

    def test_default_relays(self):
        """Transport uses default relays when none specified."""
        t = NostrTransport()
        assert t._relays == DEFAULT_RELAYS

    def test_custom_relays(self):
        """Transport accepts custom relay list."""
        t = NostrTransport(relays=["wss://custom.relay"])
        assert t._relays == ["wss://custom.relay"]

    def test_configure_overrides(self):
        """configure() updates relays and timeout."""
        t = NostrTransport()
        t.configure({"relays": ["wss://new.relay"], "relay_timeout": 10.0})
        assert t._relays == ["wss://new.relay"]
        assert t._timeout == 10.0

    def test_is_available(self):
        """is_available returns True when deps are present."""
        t = NostrTransport()
        assert t.is_available() is True

    def test_pubkey_set_on_init(self):
        """Public key hex is populated on initialization."""
        t = NostrTransport()
        assert len(t.pubkey) == 64

    def test_explicit_private_key(self):
        """Transport accepts an explicit private key."""
        secret = _random_secret()
        t = NostrTransport(private_key_hex=secret.hex())
        x, _ = _pubkey_of(secret)
        assert t.pubkey == x.hex()

    def test_extract_id(self, sample_envelope: bytes):
        """_extract_id pulls envelope_id from JSON."""
        assert NostrTransport._extract_id(sample_envelope) == "test-envelope-123"

    def test_extract_id_invalid_json(self):
        """_extract_id returns fallback for non-JSON."""
        result = NostrTransport._extract_id(b"not json")
        assert result.startswith("unknown-")


# ═══════════════════════════════════════════════════════════
# Send / receive (mocked relays)
# ═══════════════════════════════════════════════════════════


class TestSendReceive:
    """Test send and receive with mocked relay I/O."""

    def test_send_not_available(self, sample_envelope: bytes):
        """Send when unavailable returns failure."""
        t = NostrTransport()
        with patch.object(t, "is_available", return_value=False):
            result = t.send(sample_envelope, "a" * 64)
        assert result.success is False

    def test_send_with_mock_relay(self, sample_envelope: bytes):
        """Send succeeds when relay accepts the event."""
        t = NostrTransport()
        recipient = _random_secret()
        rx, _ = _pubkey_of(recipient)

        with patch("skcomm.transports.nostr._publish_to_relay", return_value=True):
            result = t.send(sample_envelope, rx.hex())
        assert result.success is True
        assert result.transport_name == "nostr"

    def test_send_all_relays_reject(self, sample_envelope: bytes):
        """Send fails when no relay accepts."""
        t = NostrTransport()
        recipient = _random_secret()
        rx, _ = _pubkey_of(recipient)

        with patch("skcomm.transports.nostr._publish_to_relay", return_value=False):
            result = t.send(sample_envelope, rx.hex())
        assert result.success is False
        assert "No relay" in result.error

    def test_receive_empty(self):
        """Receive with no matching events returns empty list."""
        t = NostrTransport()
        with patch("skcomm.transports.nostr._query_relay", return_value=[]):
            result = t.receive()
        assert result == []

    def test_receive_deduplicates(self):
        """Seen event IDs are not processed again."""
        t = NostrTransport()
        sender = _random_secret()
        sx, _ = _pubkey_of(sender)
        rx, _ = _pubkey_of(t._secret)

        envelope = json.dumps({"envelope_id": "dedup-test"}).encode()
        content_b64 = base64.b64encode(envelope).decode()
        gift = wrap_dm(sender, sx.hex(), rx.hex(), content_b64)

        with patch("skcomm.transports.nostr._query_relay", return_value=[gift]):
            first = t.receive()
        assert len(first) == 1

        with patch("skcomm.transports.nostr._query_relay", return_value=[gift]):
            second = t.receive()
        assert len(second) == 0

    def test_full_send_receive_roundtrip(self):
        """End-to-end: send from A, receive at B via mocked relay."""
        sender_t = NostrTransport()
        receiver_t = NostrTransport()

        envelope = json.dumps({"envelope_id": "e2e-test", "msg": "hello"}).encode()

        captured_events: list[dict] = []

        def mock_publish(relay_url, event, timeout=5.0):
            captured_events.append(event)
            return True

        with patch("skcomm.transports.nostr._publish_to_relay", side_effect=mock_publish):
            result = sender_t.send(envelope, receiver_t.pubkey)
        assert result.success is True

        with patch("skcomm.transports.nostr._query_relay", return_value=captured_events):
            received = receiver_t.receive()
        assert len(received) == 1
        assert json.loads(received[0])["envelope_id"] == "e2e-test"


# ═══════════════════════════════════════════════════════════
# Health check
# ═══════════════════════════════════════════════════════════


class TestHealthCheck:
    """Test health check reporting."""

    def test_health_all_relays_reachable(self):
        """Health check reports AVAILABLE when relays respond."""
        t = NostrTransport(relays=["wss://mock1", "wss://mock2"])
        with patch("skcomm.transports.nostr._ws_connect"):
            health = t.health_check()
        assert health.status == TransportStatus.AVAILABLE
        assert health.details["reachable_relays"] == 2

    def test_health_partial_relays(self):
        """Health check reports DEGRADED when some relays fail."""
        t = NostrTransport(relays=["wss://good", "wss://bad"])
        call_count = 0

        def side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "bad" in url:
                raise ConnectionError("down")
            from unittest.mock import MagicMock
            return MagicMock()

        with patch("skcomm.transports.nostr._ws_connect", side_effect=side_effect):
            health = t.health_check()
        assert health.status == TransportStatus.DEGRADED

    def test_health_no_relays_reachable(self):
        """Health check reports UNAVAILABLE when all relays fail."""
        t = NostrTransport(relays=["wss://dead1"])
        with patch("skcomm.transports.nostr._ws_connect", side_effect=ConnectionError):
            health = t.health_check()
        assert health.status == TransportStatus.UNAVAILABLE


# ═══════════════════════════════════════════════════════════
# Identity publishing
# ═══════════════════════════════════════════════════════════


class TestIdentityPublish:
    """Test PGP fingerprint publishing to Nostr metadata."""

    def test_publish_identity(self):
        """publish_identity sends kind 0 event with PGP fingerprint."""
        t = NostrTransport()
        with patch("skcomm.transports.nostr._publish_to_relay", return_value=True) as mock:
            result = t.publish_identity("ABCD1234FINGERPRINT")
        assert result is True
        event = mock.call_args[0][1]
        metadata = json.loads(event["content"])
        assert metadata["skcomm_pgp"] == "ABCD1234FINGERPRINT"
        assert event["kind"] == 0
