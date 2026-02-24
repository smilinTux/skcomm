"""Tests for SKComm router."""

import json

import pytest

from skcomm.models import (
    MessageEnvelope,
    MessagePayload,
    RoutingConfig,
    RoutingMode,
)
from skcomm.router import Router
from skcomm.transport import TransportCategory

from .conftest import MockTransport


class TestRouterBasics:
    """Tests for router registration and configuration."""

    def test_empty_router(self):
        """Expected: router with no transports returns empty list."""
        router = Router()
        assert router.transports == []

    def test_register_transport(self, mock_transport):
        """Expected: transport registered and accessible."""
        router = Router()
        router.register_transport(mock_transport)
        assert len(router.transports) == 1
        assert router.transports[0].name == "mock"

    def test_register_replaces_same_name(self, mock_transport):
        """Expected: re-registering same name replaces the old one."""
        router = Router()
        router.register_transport(mock_transport)
        new_mock = MockTransport(name="mock", priority=5)
        router.register_transport(new_mock)
        assert len(router.transports) == 1
        assert router.transports[0].priority == 5

    def test_unregister_transport(self, mock_transport):
        """Expected: transport removed by name."""
        router = Router()
        router.register_transport(mock_transport)
        assert router.unregister_transport("mock") is True
        assert len(router.transports) == 0

    def test_unregister_nonexistent(self):
        """Edge case: unregistering unknown name returns False."""
        router = Router()
        assert router.unregister_transport("ghost") is False

    def test_transports_sorted_by_priority(self):
        """Expected: transports returned in priority order."""
        router = Router()
        router.register_transport(MockTransport(name="low", priority=10))
        router.register_transport(MockTransport(name="high", priority=1))
        router.register_transport(MockTransport(name="mid", priority=5))
        names = [t.name for t in router.transports]
        assert names == ["high", "mid", "low"]


class TestRouterFailover:
    """Tests for failover routing mode."""

    def test_failover_uses_first_available(self, sample_envelope):
        """Expected: sends via highest-priority transport only."""
        t1 = MockTransport(name="primary", priority=1)
        t2 = MockTransport(name="backup", priority=2)
        router = Router(transports=[t1, t2])

        report = router.route(sample_envelope)
        assert report.delivered is True
        assert report.successful_transport == "primary"
        assert len(t1.sent) == 1
        assert len(t2.sent) == 0

    def test_failover_falls_to_second(self, sample_envelope):
        """Expected: first fails, second succeeds."""
        t1 = MockTransport(name="broken", priority=1, fail_on_send=True)
        t2 = MockTransport(name="backup", priority=2)
        router = Router(transports=[t1, t2])

        report = router.route(sample_envelope)
        assert report.delivered is True
        assert report.successful_transport == "backup"
        assert len(report.attempts) == 2

    def test_failover_all_fail(self, sample_envelope):
        """Failure: all transports fail returns undelivered report."""
        t1 = MockTransport(name="fail1", priority=1, fail_on_send=True)
        t2 = MockTransport(name="fail2", priority=2, fail_on_send=True)
        router = Router(transports=[t1, t2])

        report = router.route(sample_envelope)
        assert report.delivered is False
        assert len(report.attempts) == 2

    def test_failover_skips_unavailable(self, sample_envelope):
        """Expected: unavailable transports are not attempted."""
        t1 = MockTransport(name="down", priority=1, available=False)
        t2 = MockTransport(name="up", priority=2)
        router = Router(transports=[t1, t2])

        report = router.route(sample_envelope)
        assert report.delivered is True
        assert report.successful_transport == "up"
        assert len(report.attempts) == 1

    def test_no_available_transports(self, sample_envelope):
        """Failure: no available transports returns undelivered."""
        t1 = MockTransport(name="down1", priority=1, available=False)
        t2 = MockTransport(name="down2", priority=2, available=False)
        router = Router(transports=[t1, t2])

        report = router.route(sample_envelope)
        assert report.delivered is False
        assert len(report.attempts) == 0


class TestRouterBroadcast:
    """Tests for broadcast routing mode."""

    def test_broadcast_sends_to_all(self):
        """Expected: envelope sent via all available transports."""
        t1 = MockTransport(name="t1", priority=1)
        t2 = MockTransport(name="t2", priority=2)
        t3 = MockTransport(name="t3", priority=3, available=False)
        router = Router(transports=[t1, t2, t3])

        envelope = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="broadcast test"),
            routing=RoutingConfig(mode=RoutingMode.BROADCAST),
        )

        report = router.route(envelope)
        assert report.delivered is True
        assert len(report.attempts) == 2
        assert len(t1.sent) == 1
        assert len(t2.sent) == 1
        assert len(t3.sent) == 0

    def test_broadcast_partial_failure(self):
        """Expected: delivery succeeds if at least one transport works."""
        t1 = MockTransport(name="ok", priority=1)
        t2 = MockTransport(name="fail", priority=2, fail_on_send=True)
        router = Router(transports=[t1, t2])

        envelope = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="test"),
            routing=RoutingConfig(mode=RoutingMode.BROADCAST),
        )

        report = router.route(envelope)
        assert report.delivered is True
        assert len(report.attempts) == 2


class TestRouterStealth:
    """Tests for stealth routing mode."""

    def test_stealth_filters_to_stealth_only(self):
        """Expected: only file_based and stealth transports used."""
        t_file = MockTransport(
            name="file", priority=1, category=TransportCategory.FILE_BASED
        )
        t_tcp = MockTransport(
            name="tcp", priority=2, category=TransportCategory.REALTIME
        )
        t_stealth = MockTransport(
            name="dns", priority=3, category=TransportCategory.STEALTH
        )
        router = Router(transports=[t_file, t_tcp, t_stealth])

        envelope = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="whisper"),
            routing=RoutingConfig(mode=RoutingMode.STEALTH),
        )

        report = router.route(envelope)
        assert report.delivered is True
        assert report.successful_transport == "file"
        assert len(t_tcp.sent) == 0


class TestRouterSpeed:
    """Tests for speed routing mode."""

    def test_speed_filters_to_realtime_only(self):
        """Expected: only realtime transports used in speed mode."""
        t_file = MockTransport(
            name="file", priority=1, category=TransportCategory.FILE_BASED
        )
        t_tcp = MockTransport(
            name="tcp", priority=2, category=TransportCategory.REALTIME
        )
        router = Router(transports=[t_file, t_tcp])

        envelope = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="urgent"),
            routing=RoutingConfig(mode=RoutingMode.SPEED),
        )

        report = router.route(envelope)
        assert report.delivered is True
        assert report.successful_transport == "tcp"
        assert len(t_file.sent) == 0


class TestRouterReceive:
    """Tests for receiving and deduplication."""

    def test_receive_from_transport(self, mock_transport):
        """Expected: messages from transports are returned."""
        mock_transport.queue_message(b'{"envelope_id": "env-1"}')
        router = Router(transports=[mock_transport])

        received = router.receive_all()
        assert len(received) == 1

    def test_deduplication(self, mock_transport):
        """Expected: duplicate envelope_ids are dropped."""
        msg = json.dumps({"envelope_id": "dup-1"}).encode()
        mock_transport.queue_message(msg)
        router = Router(transports=[mock_transport])

        first = router.receive_all()
        assert len(first) == 1

        mock_transport.queue_message(msg)
        second = router.receive_all()
        assert len(second) == 0

    def test_receive_skips_unavailable(self, mock_transport, unavailable_transport):
        """Expected: unavailable transports are not polled."""
        mock_transport.queue_message(b'{"envelope_id": "good-1"}')
        unavailable_transport.queue_message(b'{"envelope_id": "missed-1"}')
        router = Router(transports=[mock_transport, unavailable_transport])

        received = router.receive_all()
        assert len(received) == 1

    def test_receive_multiple_transports(self):
        """Expected: messages from all transports aggregated."""
        t1 = MockTransport(name="t1", priority=1)
        t2 = MockTransport(name="t2", priority=2)
        t1.queue_message(json.dumps({"envelope_id": "a"}).encode())
        t2.queue_message(json.dumps({"envelope_id": "b"}).encode())
        router = Router(transports=[t1, t2])

        received = router.receive_all()
        assert len(received) == 2


class TestRouterPreferredTransports:
    """Tests for preferred transport boosting."""

    def test_preferred_transport_boosted(self):
        """Expected: preferred transport used even if lower natural priority."""
        t1 = MockTransport(name="default", priority=1)
        t2 = MockTransport(name="preferred", priority=5)
        router = Router(transports=[t1, t2])

        envelope = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="prefer this path"),
            routing=RoutingConfig(preferred_transports=["preferred"]),
        )

        report = router.route(envelope)
        assert report.delivered is True
        assert report.successful_transport == "preferred"


class TestRouterHealthReport:
    """Tests for the health report."""

    def test_health_report(self, mock_transport, unavailable_transport):
        """Expected: health report includes all transports."""
        router = Router(transports=[mock_transport, unavailable_transport])
        report = router.health_report()
        assert "mock" in report
        assert "down" in report
        assert report["mock"]["status"] == "available"
        assert report["down"]["status"] == "unavailable"
