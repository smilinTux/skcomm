"""Tests for the WebSocket transport."""

from __future__ import annotations

import json
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from skcomm.models import MessageEnvelope, MessagePayload
from skcomm.transport import TransportCategory, TransportStatus
from skcomm.transports.websocket import (
    DEFAULT_URL,
    HEARTBEAT_INTERVAL,
    WebSocketTransport,
    create_transport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_envelope(sender="opus", recipient="lumina", content="Hello WebSocket"):
    """Create a serialised MessageEnvelope for testing."""
    return MessageEnvelope(
        sender=sender,
        recipient=recipient,
        payload=MessagePayload(content=content),
    ).to_bytes()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport():
    """A WebSocketTransport that is NOT connected (no background thread)."""
    return WebSocketTransport(url="ws://localhost:9999/ws", token="test-token")


@pytest.fixture
def connected_transport():
    """A WebSocketTransport with a mocked active connection."""
    t = WebSocketTransport(url="ws://localhost:9999/ws", token="test-token", agent_name="opus")
    ws_mock = MagicMock()
    t._ws = ws_mock
    t._connected = True
    t._running = True
    return t, ws_mock


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestWebSocketTransportInit:
    """Tests for transport construction."""

    def test_default_init(self):
        """Expected: defaults are applied when no args given."""
        t = WebSocketTransport()
        assert t._url == DEFAULT_URL
        assert t._token is None
        assert t._agent_name is None
        assert t.priority == 2
        assert t._heartbeat_interval == HEARTBEAT_INTERVAL
        assert t.name == "websocket"
        assert t.category == TransportCategory.REALTIME

    def test_custom_url(self):
        """Expected: custom URL is stored."""
        t = WebSocketTransport(url="wss://relay.example.com/ws")
        assert t._url == "wss://relay.example.com/ws"

    def test_custom_token(self):
        """Expected: CapAuth token is stored."""
        t = WebSocketTransport(token="capauth-abc123")
        assert t._token == "capauth-abc123"

    def test_custom_agent_name(self):
        """Expected: agent name is stored for URL construction."""
        t = WebSocketTransport(agent_name="jarvis")
        assert t._agent_name == "jarvis"

    def test_custom_priority(self):
        """Expected: priority override is respected."""
        t = WebSocketTransport(priority=5)
        assert t.priority == 5

    def test_custom_heartbeat_interval(self):
        """Expected: heartbeat interval override is respected."""
        t = WebSocketTransport(heartbeat_interval=10)
        assert t._heartbeat_interval == 10

    def test_not_connected_on_init(self, transport):
        """Expected: not connected or running on init without auto_connect."""
        assert transport._connected is False
        assert transport._running is False
        assert transport._ws is None

    def test_auto_connect_starts_thread(self):
        """Expected: auto_connect=True starts the background thread."""
        with patch("websockets.sync.client.connect") as mock_connect:
            mock_ws = MagicMock()
            mock_ws.__enter__ = MagicMock(return_value=mock_ws)
            mock_ws.__exit__ = MagicMock(return_value=False)
            mock_ws.recv = MagicMock(side_effect=TimeoutError)
            mock_connect.return_value = mock_ws

            t = WebSocketTransport(url="ws://localhost:9999/ws", auto_connect=True)
            assert t._running is True
            t.disconnect()


# ---------------------------------------------------------------------------
# configure()
# ---------------------------------------------------------------------------


class TestWebSocketTransportConfigure:
    """Tests for configure()."""

    def test_configure_updates_url(self, transport):
        """Expected: configure() updates the server URL."""
        transport.configure({"url": "ws://new-server:8080/ws"})
        assert transport._url == "ws://new-server:8080/ws"

    def test_configure_updates_token(self, transport):
        """Expected: configure() updates the auth token."""
        transport.configure({"token": "new-token-xyz"})
        assert transport._token == "new-token-xyz"

    def test_configure_updates_agent_name(self, transport):
        """Expected: configure() updates the agent name."""
        transport.configure({"agent_name": "lumina"})
        assert transport._agent_name == "lumina"

    def test_configure_updates_priority(self, transport):
        """Expected: configure() updates the transport priority."""
        transport.configure({"priority": 3})
        assert transport.priority == 3

    def test_configure_updates_heartbeat(self, transport):
        """Expected: configure() updates heartbeat interval."""
        transport.configure({"heartbeat_interval": 60})
        assert transport._heartbeat_interval == 60


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestWebSocketTransportAvailability:
    """Tests for is_available()."""

    def test_not_available_when_disconnected(self, transport):
        """Expected: unavailable before connect() is called."""
        assert transport.is_available() is False

    def test_available_when_connected(self, connected_transport):
        """Expected: available when _connected flag is set."""
        t, _ = connected_transport
        assert t.is_available() is True

    def test_not_available_after_disconnect(self, connected_transport):
        """Expected: unavailable after disconnect() clears state."""
        t, ws_mock = connected_transport
        t._running = False
        t._connected = False
        assert t.is_available() is False


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


class TestWebSocketTransportSend:
    """Tests for send()."""

    def test_send_fails_when_not_connected(self, transport):
        """Expected: send returns failure when not connected."""
        data = make_envelope()
        result = transport.send(data, "lumina")
        assert result.success is False
        assert result.transport_name == "websocket"
        assert "Not connected" in result.error

    def test_send_succeeds_when_connected(self, connected_transport):
        """Expected: send calls ws.send() and returns success."""
        t, ws_mock = connected_transport
        data = make_envelope()
        result = t.send(data, "lumina")

        assert result.success is True
        assert result.transport_name == "websocket"
        assert result.latency_ms >= 0
        ws_mock.send.assert_called_once_with(data)

    def test_send_includes_envelope_id(self, connected_transport):
        """Expected: send result includes the envelope_id."""
        t, _ = connected_transport
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="test"),
        )
        result = t.send(env.to_bytes(), "lumina")
        assert result.envelope_id == env.envelope_id

    def test_send_handles_ws_exception(self, connected_transport):
        """Expected: send returns failure and marks disconnected on WS error."""
        t, ws_mock = connected_transport
        ws_mock.send.side_effect = ConnectionError("connection lost")

        data = make_envelope()
        result = t.send(data, "lumina")

        assert result.success is False
        assert "connection lost" in result.error
        assert t._connected is False

    def test_send_records_latency(self, connected_transport):
        """Expected: send result includes non-negative latency."""
        t, _ = connected_transport
        result = t.send(make_envelope(), "lumina")
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# receive()
# ---------------------------------------------------------------------------


class TestWebSocketTransportReceive:
    """Tests for receive()."""

    def test_receive_empty_inbox(self, transport):
        """Expected: returns empty list when no messages queued."""
        assert transport.receive() == []

    def test_receive_drains_queue(self, transport):
        """Expected: returns all queued messages and clears the queue."""
        env1 = make_envelope(content="msg-1")
        env2 = make_envelope(content="msg-2")
        transport._inbox.put(env1)
        transport._inbox.put(env2)

        received = transport.receive()
        assert len(received) == 2
        assert env1 in received
        assert env2 in received

    def test_receive_clears_after_drain(self, transport):
        """Expected: second receive() returns empty after first drains."""
        transport._inbox.put(make_envelope())
        transport.receive()
        assert transport.receive() == []

    def test_receive_parses_envelope(self, transport):
        """Expected: queued bytes are valid MessageEnvelope."""
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="ws message"),
        )
        transport._inbox.put(env.to_bytes())

        received = transport.receive()
        assert len(received) == 1
        parsed = MessageEnvelope.from_bytes(received[0])
        assert parsed.sender == "opus"
        assert parsed.payload.content == "ws message"


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


class TestWebSocketTransportHealthCheck:
    """Tests for health_check()."""

    def test_health_not_started(self, transport):
        """Expected: UNAVAILABLE when transport not started."""
        health = transport.health_check()
        assert health.status == TransportStatus.UNAVAILABLE
        assert health.transport_name == "websocket"
        assert health.error is not None

    def test_health_running_but_disconnected(self, transport):
        """Expected: DEGRADED when running but not connected."""
        transport._running = True
        health = transport.health_check()
        assert health.status == TransportStatus.DEGRADED

    def test_health_connected_and_ping_succeeds(self, connected_transport):
        """Expected: AVAILABLE when connected and ping succeeds."""
        t, ws_mock = connected_transport
        ws_mock.ping.return_value = None

        health = t.health_check()
        assert health.status == TransportStatus.AVAILABLE
        assert health.latency_ms is not None
        assert health.details["connected"] is True
        assert "url" in health.details

    def test_health_reports_pending_inbox(self, connected_transport):
        """Expected: health details include pending inbox count."""
        t, ws_mock = connected_transport
        ws_mock.ping.return_value = None
        t._inbox.put(make_envelope())
        t._inbox.put(make_envelope())

        health = t.health_check()
        assert health.details["pending_inbox"] == 2

    def test_health_ping_failure_is_degraded(self, connected_transport):
        """Expected: DEGRADED when ping raises an exception."""
        t, ws_mock = connected_transport
        ws_mock.ping.side_effect = ConnectionError("lost")

        health = t.health_check()
        assert health.status == TransportStatus.DEGRADED
        assert "Ping failed" in health.error

    def test_health_reports_reconnect_count(self, transport):
        """Expected: reconnect_count appears in DEGRADED details."""
        transport._running = True
        transport._reconnect_count = 3
        health = transport.health_check()
        assert health.details.get("reconnect_count") == 3


# ---------------------------------------------------------------------------
# URL and header construction
# ---------------------------------------------------------------------------


class TestWebSocketTransportHelpers:
    """Tests for URL building, header construction, and _extract_id."""

    def test_build_url_no_agent(self):
        """Expected: URL unchanged when agent_name is None."""
        t = WebSocketTransport(url="ws://host/ws")
        assert t._build_url() == "ws://host/ws"

    def test_build_url_with_agent(self):
        """Expected: agent name appended as query param."""
        t = WebSocketTransport(url="ws://host/ws", agent_name="opus")
        assert t._build_url() == "ws://host/ws?agent=opus"

    def test_build_url_with_existing_query(self):
        """Expected: agent appended with & when URL already has query."""
        t = WebSocketTransport(url="ws://host/ws?version=1", agent_name="lumina")
        assert t._build_url() == "ws://host/ws?version=1&agent=lumina"

    def test_build_headers_no_token(self):
        """Expected: empty headers when no token."""
        t = WebSocketTransport()
        assert t._build_headers() == {}

    def test_build_headers_with_token(self):
        """Expected: Authorization header added with Bearer token."""
        t = WebSocketTransport(token="abc-token")
        headers = t._build_headers()
        assert headers["Authorization"] == "Bearer abc-token"

    def test_extract_id_valid_json(self):
        """Expected: extracts envelope_id from valid JSON envelope."""
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="test"),
        )
        eid = WebSocketTransport._extract_id(env.to_bytes())
        assert eid == env.envelope_id

    def test_extract_id_fallback_on_garbage(self):
        """Expected: fallback ID for non-JSON data."""
        eid = WebSocketTransport._extract_id(b"\x00\x01\x02 not json")
        assert eid.startswith("unknown-")

    def test_extract_id_missing_key(self):
        """Expected: fallback ID when envelope_id key absent."""
        data = json.dumps({"foo": "bar"}).encode()
        eid = WebSocketTransport._extract_id(data)
        assert eid.startswith("unknown-")


# ---------------------------------------------------------------------------
# disconnect() lifecycle
# ---------------------------------------------------------------------------


class TestWebSocketTransportLifecycle:
    """Tests for connect/disconnect lifecycle."""

    def test_disconnect_stops_running(self, connected_transport):
        """Expected: disconnect() sets _running to False."""
        t, ws_mock = connected_transport
        t.disconnect()
        assert t._running is False

    def test_disconnect_clears_ws(self, connected_transport):
        """Expected: disconnect() closes and clears the WS reference."""
        t, ws_mock = connected_transport
        t.disconnect()
        assert t._ws is None
        ws_mock.close.assert_called_once()

    def test_disconnect_idempotent(self, transport):
        """Expected: calling disconnect() on a stopped transport is safe."""
        transport.disconnect()  # Should not raise
        assert transport._running is False

    def test_connect_is_idempotent(self):
        """Expected: calling connect() twice does not start extra threads."""
        t = WebSocketTransport(url="ws://localhost:9999/ws")
        with patch.object(t, "_receiver_loop"):
            t._running = True  # Simulate already running
            result = t.connect()
            assert result is True
            # No new thread started


# ---------------------------------------------------------------------------
# Background receiver: string-to-bytes coercion
# ---------------------------------------------------------------------------


class TestWebSocketReceiverCoercion:
    """Tests for string→bytes coercion in the receiver loop."""

    def test_string_message_coerced_to_bytes(self):
        """Expected: string messages from server are converted to bytes."""
        t = WebSocketTransport(url="ws://localhost:9999/ws")
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="coercion test"),
        )
        # Simulate the receiver receiving a str (some servers send JSON as text)
        t._inbox.put(env.to_bytes())  # Already bytes here; conversion is in loop

        received = t.receive()
        assert len(received) == 1
        assert isinstance(received[0], bytes)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestCreateTransportFactory:
    """Tests for the create_transport factory."""

    def test_factory_defaults(self):
        """Expected: factory creates transport with default settings."""
        t = create_transport()
        assert isinstance(t, WebSocketTransport)
        assert t._url == DEFAULT_URL
        assert t.priority == 2

    def test_factory_custom_url(self):
        """Expected: factory passes custom URL through."""
        t = create_transport(url="wss://prod.example.com/ws")
        assert t._url == "wss://prod.example.com/ws"

    def test_factory_custom_token(self):
        """Expected: factory passes CapAuth token through."""
        t = create_transport(token="my-capauth-token")
        assert t._token == "my-capauth-token"

    def test_factory_custom_agent(self):
        """Expected: factory passes agent_name through."""
        t = create_transport(agent_name="jarvis")
        assert t._agent_name == "jarvis"

    def test_factory_custom_priority(self):
        """Expected: factory passes priority through."""
        t = create_transport(priority=4)
        assert t.priority == 4

    def test_factory_custom_heartbeat(self):
        """Expected: factory passes heartbeat_interval through."""
        t = create_transport(heartbeat_interval=15)
        assert t._heartbeat_interval == 15

    def test_factory_no_auto_connect(self):
        """Expected: factory does not connect by default."""
        t = create_transport()
        assert t._running is False
        assert t._connected is False
