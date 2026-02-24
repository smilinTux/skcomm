"""Tests for SKComm message models."""

from datetime import datetime, timedelta, timezone

import pytest

from skcomm.models import (
    MessageEnvelope,
    MessageMetadata,
    MessagePayload,
    MessageType,
    RoutingConfig,
    RoutingMode,
    Urgency,
)


class TestMessagePayload:
    """Tests for the MessagePayload model."""

    def test_default_payload(self):
        """Expected: payload with TEXT type, not encrypted."""
        payload = MessagePayload(content="Hello world")
        assert payload.content == "Hello world"
        assert payload.content_type == MessageType.TEXT
        assert payload.encrypted is False
        assert payload.compressed is False
        assert payload.signature is None

    def test_encrypted_payload(self):
        """Expected: payload with encrypted ciphertext."""
        payload = MessagePayload(
            content="-----BEGIN PGP MESSAGE-----\nencrypted data\n-----END PGP MESSAGE-----",
            encrypted=True,
            signature="deadbeef",
        )
        assert payload.encrypted is True
        assert payload.signature == "deadbeef"

    def test_all_message_types(self):
        """Edge case: every content type should be valid."""
        for mtype in MessageType:
            payload = MessagePayload(content="test", content_type=mtype)
            assert payload.content_type == mtype


class TestRoutingConfig:
    """Tests for the RoutingConfig model."""

    def test_default_routing(self):
        """Expected: failover mode with standard retry backoff."""
        routing = RoutingConfig()
        assert routing.mode == RoutingMode.FAILOVER
        assert routing.retry_max == 5
        assert routing.retry_backoff == [5, 15, 60, 300, 900]
        assert routing.ttl == 86400
        assert routing.ack_requested is True

    def test_broadcast_mode(self):
        """Expected: broadcast mode with custom retries."""
        routing = RoutingConfig(
            mode=RoutingMode.BROADCAST,
            retry_max=2,
            preferred_transports=["syncthing", "nostr"],
        )
        assert routing.mode == RoutingMode.BROADCAST
        assert routing.retry_max == 2
        assert routing.preferred_transports == ["syncthing", "nostr"]


class TestMessageMetadata:
    """Tests for the MessageMetadata model."""

    def test_default_metadata(self):
        """Expected: normal urgency, auto-generated timestamp."""
        meta = MessageMetadata()
        assert meta.urgency == Urgency.NORMAL
        assert meta.thread_id is None
        assert meta.in_reply_to is None
        assert meta.attempt == 0
        assert isinstance(meta.created_at, datetime)

    def test_threaded_metadata(self):
        """Expected: metadata with thread and reply references."""
        meta = MessageMetadata(
            thread_id="thread-001",
            in_reply_to="envelope-abc",
            urgency=Urgency.CRITICAL,
        )
        assert meta.thread_id == "thread-001"
        assert meta.in_reply_to == "envelope-abc"
        assert meta.urgency == Urgency.CRITICAL


class TestMessageEnvelope:
    """Tests for the MessageEnvelope model."""

    @pytest.fixture
    def basic_envelope(self):
        """A minimal valid envelope."""
        return MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="Hello from Opus"),
        )

    def test_envelope_creation(self, basic_envelope):
        """Expected: envelope with auto-generated UUID and version."""
        assert basic_envelope.sender == "opus"
        assert basic_envelope.recipient == "lumina"
        assert basic_envelope.payload.content == "Hello from Opus"
        assert basic_envelope.skcomm_version == "1.0.0"
        assert len(basic_envelope.envelope_id) == 36  # UUID format

    def test_unique_envelope_ids(self):
        """Expected: each envelope gets a distinct UUID."""
        e1 = MessageEnvelope(
            sender="a", recipient="b", payload=MessagePayload(content="x")
        )
        e2 = MessageEnvelope(
            sender="a", recipient="b", payload=MessagePayload(content="x")
        )
        assert e1.envelope_id != e2.envelope_id

    def test_serialization_roundtrip(self, basic_envelope):
        """Expected: envelope survives bytes serialization and back."""
        data = basic_envelope.to_bytes()
        assert isinstance(data, bytes)

        restored = MessageEnvelope.from_bytes(data)
        assert restored.sender == basic_envelope.sender
        assert restored.recipient == basic_envelope.recipient
        assert restored.payload.content == basic_envelope.payload.content
        assert restored.envelope_id == basic_envelope.envelope_id

    def test_from_bytes_invalid(self):
        """Failure: invalid bytes should raise ValueError."""
        with pytest.raises(Exception):
            MessageEnvelope.from_bytes(b"not json at all")

    def test_make_ack(self, basic_envelope):
        """Expected: ACK envelope references the original."""
        ack = basic_envelope.make_ack(sender="lumina")
        assert ack.sender == "lumina"
        assert ack.recipient == "opus"
        assert ack.payload.content_type == MessageType.ACK
        assert ack.payload.content == basic_envelope.envelope_id
        assert ack.metadata.in_reply_to == basic_envelope.envelope_id
        assert ack.routing.ack_requested is False

    def test_is_ack(self, basic_envelope):
        """Expected: only ACK envelopes return True for is_ack."""
        assert basic_envelope.is_ack is False
        ack = basic_envelope.make_ack(sender="lumina")
        assert ack.is_ack is True

    def test_is_expired_by_ttl(self):
        """Expected: envelope older than TTL is expired."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        env = MessageEnvelope(
            sender="a",
            recipient="b",
            payload=MessagePayload(content="old"),
            routing=RoutingConfig(ttl=86400),
            metadata=MessageMetadata(created_at=old_time),
        )
        assert env.is_expired is True

    def test_is_not_expired(self):
        """Expected: fresh envelope is not expired."""
        env = MessageEnvelope(
            sender="a",
            recipient="b",
            payload=MessagePayload(content="fresh"),
        )
        assert env.is_expired is False

    def test_is_expired_by_explicit_deadline(self):
        """Expected: envelope past expires_at is expired."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        env = MessageEnvelope(
            sender="a",
            recipient="b",
            payload=MessagePayload(content="past deadline"),
            metadata=MessageMetadata(expires_at=past),
        )
        assert env.is_expired is True

    def test_full_envelope_json(self):
        """Expected: all fields present in JSON output."""
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(
                content="test",
                content_type=MessageType.SEED,
                encrypted=True,
                signature="sig123",
            ),
            routing=RoutingConfig(
                mode=RoutingMode.BROADCAST,
                preferred_transports=["syncthing"],
            ),
            metadata=MessageMetadata(
                thread_id="thread-1",
                urgency=Urgency.HIGH,
            ),
        )
        data = env.model_dump(mode="json")
        assert data["sender"] == "opus"
        assert data["payload"]["content_type"] == "seed"
        assert data["payload"]["encrypted"] is True
        assert data["routing"]["mode"] == "broadcast"
        assert data["metadata"]["thread_id"] == "thread-1"
