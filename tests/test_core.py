"""Tests for the SKComm main class."""

import json

import pytest

from skcomm.config import SKCommConfig
from skcomm.core import SKComm
from skcomm.models import (
    MessageEnvelope,
    MessagePayload,
    MessageType,
    RoutingMode,
    Urgency,
)
from skcomm.router import Router

from .conftest import MockTransport


class TestSKCommInit:
    """Tests for SKComm initialization."""

    def test_default_init(self):
        """Expected: SKComm initializes with default config."""
        comm = SKComm()
        assert comm.identity == "unknown"
        assert len(comm.router.transports) == 0

    def test_init_with_config(self):
        """Expected: SKComm uses provided config."""
        from skcomm.config import IdentityConfig

        config = SKCommConfig(
            identity=IdentityConfig(name="opus"),
            default_mode=RoutingMode.BROADCAST,
        )
        comm = SKComm(config=config)
        assert comm.identity == "opus"

    def test_init_with_router(self):
        """Expected: SKComm uses provided router."""
        router = Router()
        router.register_transport(MockTransport(name="test", priority=1))
        comm = SKComm(router=router)
        assert len(comm.router.transports) == 1


class TestSKCommSend:
    """Tests for sending messages."""

    @pytest.fixture
    def comm(self):
        """SKComm with a mock transport."""
        from skcomm.config import IdentityConfig

        config = SKCommConfig(identity=IdentityConfig(name="opus"))
        router = Router()
        router.register_transport(MockTransport(name="mock", priority=1))
        return SKComm(config=config, router=router)

    def test_send_text(self, comm):
        """Expected: text message delivered via mock transport."""
        report = comm.send("lumina", "Hello!")
        assert report.delivered is True
        assert report.successful_transport == "mock"

    def test_send_with_thread(self, comm):
        """Expected: message sent with thread context."""
        report = comm.send(
            "lumina",
            "Reply in thread",
            thread_id="thread-001",
            in_reply_to="prev-envelope",
        )
        assert report.delivered is True

    def test_send_with_urgency(self, comm):
        """Expected: message sent with critical urgency."""
        report = comm.send("lumina", "URGENT", urgency=Urgency.CRITICAL)
        assert report.delivered is True

    def test_send_seed_type(self, comm):
        """Expected: seed-type message delivered."""
        report = comm.send(
            "lumina",
            '{"seed": "data"}',
            message_type=MessageType.SEED,
        )
        assert report.delivered is True

    def test_send_with_mode_override(self, comm):
        """Expected: routing mode can be overridden per-message."""
        report = comm.send(
            "lumina", "test", mode=RoutingMode.BROADCAST
        )
        assert report.delivered is True

    def test_send_no_transports(self):
        """Failure: no transports means delivery fails."""
        comm = SKComm()
        report = comm.send("lumina", "Nobody home")
        assert report.delivered is False

    def test_send_all_fail(self):
        """Failure: all transports failing means delivery fails."""
        router = Router()
        router.register_transport(
            MockTransport(name="fail", priority=1, fail_on_send=True)
        )
        comm = SKComm(router=router)
        report = comm.send("lumina", "This won't make it")
        assert report.delivered is False


class TestSKCommSendEnvelope:
    """Tests for sending pre-built envelopes."""

    def test_send_prebuilt_envelope(self):
        """Expected: pre-built envelope routed directly."""
        router = Router()
        mock = MockTransport(name="mock", priority=1)
        router.register_transport(mock)
        comm = SKComm(router=router)

        envelope = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="pre-built"),
        )
        report = comm.send_envelope(envelope)
        assert report.delivered is True
        assert len(mock.sent) == 1


class TestSKCommReceive:
    """Tests for receiving messages."""

    def test_receive_messages(self):
        """Expected: queued messages are received and deserialized."""
        mock = MockTransport(name="mock", priority=1)
        envelope = MessageEnvelope(
            sender="lumina",
            recipient="opus",
            payload=MessagePayload(content="Hello Opus"),
        )
        mock.queue_message(envelope.to_bytes())

        router = Router(transports=[mock])
        comm = SKComm(router=router)

        messages = comm.receive()
        assert len(messages) == 1
        assert messages[0].sender == "lumina"
        assert messages[0].payload.content == "Hello Opus"

    def test_receive_skips_invalid(self):
        """Expected: malformed bytes are skipped gracefully."""
        mock = MockTransport(name="mock", priority=1)
        mock.queue_message(b"not valid json")
        mock.queue_message(
            MessageEnvelope(
                sender="opus",
                recipient="lumina",
                payload=MessagePayload(content="valid"),
            ).to_bytes()
        )

        router = Router(transports=[mock])
        comm = SKComm(router=router)

        messages = comm.receive()
        assert len(messages) == 1
        assert messages[0].payload.content == "valid"

    def test_receive_empty(self):
        """Expected: no messages returns empty list."""
        router = Router(transports=[MockTransport(name="empty", priority=1)])
        comm = SKComm(router=router)
        assert comm.receive() == []


class TestSKCommStatus:
    """Tests for the status report."""

    def test_status_report(self):
        """Expected: status includes identity and transport info."""
        from skcomm.config import IdentityConfig

        config = SKCommConfig(identity=IdentityConfig(name="opus"))
        mock = MockTransport(name="syncthing", priority=1)
        router = Router(transports=[mock])
        comm = SKComm(config=config, router=router)

        status = comm.status()
        assert status["version"] == "1.0.0"
        assert status["identity"]["name"] == "opus"
        assert status["transport_count"] == 1
        assert "syncthing" in status["transports"]
        assert status["encrypt"] is True
        assert status["sign"] is True


class TestSKCommRegisterTransport:
    """Tests for runtime transport registration."""

    def test_register_at_runtime(self):
        """Expected: transport added after init is usable."""
        comm = SKComm()
        assert len(comm.router.transports) == 0

        mock = MockTransport(name="late", priority=1)
        comm.register_transport(mock)
        assert len(comm.router.transports) == 1

        report = comm.send("lumina", "Late arrival")
        assert report.delivered is True
