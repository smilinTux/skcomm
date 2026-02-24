"""Tests for SKComm transport layer."""

import pytest

from skcomm.transport import (
    DeliveryReport,
    HealthStatus,
    SendResult,
    Transport,
    TransportCategory,
    TransportStatus,
)


class TestTransportStatus:
    """Tests for transport status enums."""

    def test_status_values(self):
        """Expected: all three status values exist."""
        assert TransportStatus.AVAILABLE == "available"
        assert TransportStatus.DEGRADED == "degraded"
        assert TransportStatus.UNAVAILABLE == "unavailable"

    def test_category_values(self):
        """Expected: all four category values exist."""
        assert TransportCategory.REALTIME == "realtime"
        assert TransportCategory.FILE_BASED == "file_based"
        assert TransportCategory.STEALTH == "stealth"
        assert TransportCategory.OFFLINE == "offline"


class TestHealthStatus:
    """Tests for the HealthStatus model."""

    def test_healthy_status(self):
        """Expected: available transport with latency."""
        health = HealthStatus(
            transport_name="syncthing",
            status=TransportStatus.AVAILABLE,
            latency_ms=15.2,
        )
        assert health.transport_name == "syncthing"
        assert health.status == TransportStatus.AVAILABLE
        assert health.latency_ms == 15.2
        assert health.error is None

    def test_error_status(self):
        """Expected: unavailable transport with error message."""
        health = HealthStatus(
            transport_name="ssh",
            status=TransportStatus.UNAVAILABLE,
            error="Connection refused",
        )
        assert health.status == TransportStatus.UNAVAILABLE
        assert health.error == "Connection refused"
        assert health.latency_ms is None


class TestSendResult:
    """Tests for the SendResult model."""

    def test_successful_send(self):
        """Expected: successful send with timing data."""
        result = SendResult(
            success=True,
            transport_name="file",
            envelope_id="abc-123",
            latency_ms=5.0,
        )
        assert result.success is True
        assert result.transport_name == "file"
        assert result.envelope_id == "abc-123"
        assert result.error is None

    def test_failed_send(self):
        """Expected: failed send with error message."""
        result = SendResult(
            success=False,
            transport_name="netcat",
            envelope_id="def-456",
            latency_ms=5000.0,
            error="Connection timeout",
        )
        assert result.success is False
        assert result.error == "Connection timeout"


class TestDeliveryReport:
    """Tests for the DeliveryReport model."""

    def test_empty_report(self):
        """Expected: no attempts means not delivered."""
        report = DeliveryReport(envelope_id="test-1", delivered=False)
        assert report.delivered is False
        assert report.successful_transport is None
        assert report.attempts == []

    def test_successful_delivery(self):
        """Expected: report with successful attempt shows transport name."""
        report = DeliveryReport(
            envelope_id="test-2",
            delivered=True,
            attempts=[
                SendResult(
                    success=True,
                    transport_name="syncthing",
                    envelope_id="test-2",
                    latency_ms=3.0,
                )
            ],
        )
        assert report.delivered is True
        assert report.successful_transport == "syncthing"

    def test_failover_delivery(self):
        """Expected: first transport fails, second succeeds."""
        report = DeliveryReport(
            envelope_id="test-3",
            delivered=True,
            attempts=[
                SendResult(
                    success=False,
                    transport_name="ssh",
                    envelope_id="test-3",
                    error="Connection refused",
                ),
                SendResult(
                    success=True,
                    transport_name="file",
                    envelope_id="test-3",
                    latency_ms=2.0,
                ),
            ],
        )
        assert report.delivered is True
        assert report.successful_transport == "file"


class TestTransportABC:
    """Tests for the Transport abstract base class."""

    def test_cannot_instantiate_abc(self):
        """Failure: direct instantiation of Transport should fail."""
        with pytest.raises(TypeError):
            Transport()

    def test_mock_transport_is_transport(self, mock_transport):
        """Expected: mock transport is a valid Transport subclass."""
        assert isinstance(mock_transport, Transport)
        assert mock_transport.name == "mock"
        assert mock_transport.priority == 1

    def test_mock_send(self, mock_transport):
        """Expected: mock transport records sent data."""
        result = mock_transport.send(b"test envelope", "lumina")
        assert result.success is True
        assert len(mock_transport.sent) == 1
        assert mock_transport.sent[0] == (b"test envelope", "lumina")

    def test_mock_receive(self, mock_transport):
        """Expected: mock transport returns queued messages."""
        mock_transport.queue_message(b"incoming-1")
        mock_transport.queue_message(b"incoming-2")
        messages = mock_transport.receive()
        assert len(messages) == 2
        assert mock_transport.receive() == []  # Inbox cleared

    def test_mock_health_check(self, mock_transport):
        """Expected: available transport reports healthy."""
        health = mock_transport.health_check()
        assert health.status == TransportStatus.AVAILABLE
        assert health.latency_ms == 1.0

    def test_failing_transport(self, failing_transport):
        """Expected: failing transport returns error on send."""
        result = failing_transport.send(b"test", "lumina")
        assert result.success is False
        assert "configured to fail" in result.error
