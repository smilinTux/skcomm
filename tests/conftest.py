"""
Shared test fixtures for SKComm tests.

Provides a mock transport implementation that records all
send/receive calls for assertion in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from skcomm.models import MessageEnvelope, MessagePayload
from skcomm.transport import (
    DeliveryReport,
    HealthStatus,
    SendResult,
    Transport,
    TransportCategory,
    TransportStatus,
)


class MockTransport(Transport):
    """In-memory transport for testing.

    Records all sent envelopes and serves queued receive data.
    """

    def __init__(
        self,
        name: str = "mock",
        priority: int = 1,
        available: bool = True,
        fail_on_send: bool = False,
        category: TransportCategory = TransportCategory.FILE_BASED,
    ):
        self.name = name
        self.priority = priority
        self.category = category
        self._available = available
        self._fail_on_send = fail_on_send
        self.sent: list[tuple[bytes, str]] = []
        self._inbox: list[bytes] = []

    def configure(self, config: dict) -> None:
        pass

    def is_available(self) -> bool:
        return self._available

    def send(self, envelope_bytes: bytes, recipient: str) -> SendResult:
        if self._fail_on_send:
            return SendResult(
                success=False,
                transport_name=self.name,
                envelope_id="",
                latency_ms=0.0,
                error="Mock transport configured to fail",
            )
        self.sent.append((envelope_bytes, recipient))
        return SendResult(
            success=True,
            transport_name=self.name,
            envelope_id="",
            latency_ms=1.0,
        )

    def receive(self) -> list[bytes]:
        messages = list(self._inbox)
        self._inbox.clear()
        return messages

    def health_check(self) -> HealthStatus:
        return HealthStatus(
            transport_name=self.name,
            status=TransportStatus.AVAILABLE if self._available else TransportStatus.UNAVAILABLE,
            latency_ms=1.0 if self._available else None,
        )

    def queue_message(self, data: bytes) -> None:
        """Queue a message for the next receive() call."""
        self._inbox.append(data)


@pytest.fixture
def mock_transport():
    """A working mock transport."""
    return MockTransport(name="mock", priority=1)


@pytest.fixture
def failing_transport():
    """A mock transport that fails on every send."""
    return MockTransport(name="failing", priority=2, fail_on_send=True)


@pytest.fixture
def unavailable_transport():
    """A mock transport that reports as unavailable."""
    return MockTransport(name="down", priority=3, available=False)


@pytest.fixture
def sample_envelope():
    """A simple test envelope."""
    return MessageEnvelope(
        sender="opus",
        recipient="lumina",
        payload=MessagePayload(content="Hello from tests"),
    )
