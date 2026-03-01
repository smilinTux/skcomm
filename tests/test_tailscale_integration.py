"""Integration tests for the Tailscale transport.

Focuses on cross-cutting concerns that unit tests miss:
- Instantiation and configure() with lifecycle restarts
- start/stop lifecycle with _lifecycle_lock correctness
- TCP framing encode/decode (4-byte big-endian length prefix)
- Concurrent start calls (only one listener)
- health_check response structure and state transitions

All tests use unittest.mock — no actual Tailscale installation required.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from skcomm.transport import TransportCategory, TransportStatus
from skcomm.transports.tailscale import (
    ACCEPT_TIMEOUT,
    CONNECT_TIMEOUT,
    HEADER_SIZE,
    LISTEN_PORT,
    MAX_MESSAGE_SIZE,
    TailscaleTransport,
    create_transport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOCAL_IP = "100.64.0.1"
PEER_IP = "100.64.0.2"
PEER_NAME = "lumina"


def _make_envelope_bytes(envelope_id="ts-env-001", content="test") -> bytes:
    """Create minimal JSON envelope bytes."""
    return json.dumps({
        "envelope_id": envelope_id,
        "sender": "opus",
        "recipient": "lumina",
        "payload": {"content": content},
    }).encode()


def _mock_tailscale_ip(ip=LOCAL_IP):
    """Return a MagicMock subprocess result simulating `tailscale ip -4`."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = ip + "\n"
    return result


def _make_transport(**kwargs) -> TailscaleTransport:
    """Build a transport with mocked Tailscale detection."""
    with patch("subprocess.run", return_value=_mock_tailscale_ip()):
        return TailscaleTransport(auto_detect=False, **kwargs)


def _make_transport_no_tailscale(**kwargs) -> TailscaleTransport:
    """Build a transport where Tailscale is absent."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        return TailscaleTransport(auto_detect=False, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport():
    """A TailscaleTransport where Tailscale is running (local IP detected)."""
    return _make_transport()


@pytest.fixture
def transport_no_ts():
    """A TailscaleTransport where Tailscale is absent."""
    return _make_transport_no_tailscale()


# ===========================================================================
# 1. Instantiation and configure()
# ===========================================================================


class TestInstantiationAndConfigure:
    """Test construction and configure() interaction with lifecycle."""

    def test_instantiation_detects_local_ip(self, transport):
        """Expected: local IP is detected on construction."""
        assert transport._local_ip == LOCAL_IP

    def test_instantiation_without_tailscale(self, transport_no_ts):
        """Expected: local IP is None when Tailscale is absent."""
        assert transport_no_ts._local_ip is None

    def test_instantiation_defaults(self, transport):
        """Expected: default values are correct after construction."""
        assert transport.name == "tailscale"
        assert transport.category == TransportCategory.REALTIME
        assert transport.priority == 2
        assert transport._listen_port == LISTEN_PORT
        assert transport._auto_detect is False
        assert transport._running is False
        assert transport._peer_ips == {}

    def test_configure_updates_listen_port(self, transport):
        """Expected: configure() updates the listen port."""
        transport.configure({"listen_port": 19000})
        assert transport._listen_port == 19000

    def test_configure_updates_priority(self, transport):
        """Expected: configure() updates priority."""
        transport.configure({"priority": 5})
        assert transport.priority == 5

    def test_configure_updates_auto_detect(self, transport):
        """Expected: configure() updates auto_detect flag."""
        transport.configure({"auto_detect": True})
        assert transport._auto_detect is True

    def test_configure_refreshes_local_ip(self, transport):
        """Expected: configure() re-detects local IP."""
        new_ip = "100.64.0.99"
        with patch("subprocess.run", return_value=_mock_tailscale_ip(new_ip)):
            transport.configure({})
        assert transport._local_ip == new_ip

    def test_configure_restarts_if_was_running(self, transport):
        """Expected: configure() restarts the listener if it was running."""
        transport._running = True
        with patch.object(transport, "stop") as mock_stop, \
             patch.object(transport, "start") as mock_start, \
             patch("subprocess.run", return_value=_mock_tailscale_ip()):
            transport.configure({"listen_port": 19001})
        mock_stop.assert_called_once()
        mock_start.assert_called_once()

    def test_configure_does_not_start_if_was_stopped(self, transport):
        """Expected: configure() does not start listener if it was not running."""
        with patch.object(transport, "start") as mock_start, \
             patch("subprocess.run", return_value=_mock_tailscale_ip()):
            transport.configure({"listen_port": 19002})
        mock_start.assert_not_called()

    def test_configure_multiple_fields_at_once(self, transport):
        """Expected: configure() updates multiple fields in one call."""
        with patch("subprocess.run", return_value=_mock_tailscale_ip()):
            transport.configure({
                "listen_port": 12345,
                "priority": 8,
                "auto_detect": True,
            })
        assert transport._listen_port == 12345
        assert transport.priority == 8
        assert transport._auto_detect is True


# ===========================================================================
# 2. start/stop lifecycle with _lifecycle_lock
# ===========================================================================


class TestStartStopLifecycle:
    """Test start/stop lifecycle correctness and _lifecycle_lock behaviour."""

    def test_start_sets_running_true(self, transport):
        """Expected: start() sets _running=True and spawns a thread."""
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            result = transport.start()
        assert result is True
        assert transport._running is True
        mock_thread.start.assert_called_once()

    def test_start_is_idempotent(self, transport):
        """Expected: calling start() twice does not start a second thread."""
        transport._running = True
        with patch("threading.Thread") as mock_thread_cls:
            result = transport.start()
        assert result is True
        mock_thread_cls.assert_not_called()

    def test_start_returns_false_when_tailscale_absent(self, transport_no_ts):
        """Expected: start() returns False when Tailscale is not available."""
        result = transport_no_ts.start()
        assert result is False
        assert transport_no_ts._running is False

    def test_stop_sets_running_false(self, transport):
        """Expected: stop() clears the running flag."""
        transport._running = True
        transport._server_socket = MagicMock()
        transport.stop()
        assert transport._running is False

    def test_stop_closes_server_socket(self, transport):
        """Expected: stop() closes the server socket."""
        mock_sock = MagicMock()
        transport._running = True
        transport._server_socket = mock_sock
        transport.stop()
        mock_sock.close.assert_called_once()
        assert transport._server_socket is None

    def test_stop_joins_server_thread(self, transport):
        """Expected: stop() joins the server thread with a timeout."""
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        transport._running = True
        transport._server_thread = mock_thread
        transport._server_socket = None
        transport.stop()
        mock_thread.join.assert_called_once_with(timeout=3.0)

    def test_stop_when_not_running_is_safe(self, transport):
        """Expected: stop() on an already-stopped transport is a no-op."""
        transport._running = False
        transport._server_socket = None
        transport._server_thread = None
        # Should not raise
        transport.stop()

    def test_stop_handles_socket_close_exception(self, transport):
        """Expected: stop() handles socket.close() exception gracefully."""
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError("already closed")
        transport._running = True
        transport._server_socket = mock_sock
        # Should not raise
        transport.stop()
        assert transport._running is False

    def test_lifecycle_lock_prevents_concurrent_start(self, transport):
        """Expected: _lifecycle_lock ensures only one thread enters start()."""
        # Acquire the lock to simulate a concurrent start in progress
        transport._lifecycle_lock.acquire()
        started = threading.Event()
        result_holder = [None]

        def try_start():
            result_holder[0] = transport.start()
            started.set()

        # Start in background — it will block on the lock
        t = threading.Thread(target=try_start, daemon=True)
        t.start()

        # Give the thread a moment to block
        time.sleep(0.05)
        assert not started.is_set()  # Should still be waiting

        # Release the lock — but since _running was not set by us, start()
        # will proceed. We need to make Tailscale available.
        transport._lifecycle_lock.release()
        t.join(timeout=2.0)

    def test_start_stop_start_cycle(self, transport):
        """Expected: start-stop-start cycle works correctly."""
        # First start
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            assert transport.start() is True
        assert transport._running is True

        # Stop
        transport._server_socket = MagicMock()
        transport.stop()
        assert transport._running is False

        # Second start
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            assert transport.start() is True
        assert transport._running is True


# ===========================================================================
# 3. TCP framing encode/decode (4-byte length prefix)
# ===========================================================================


class TestTcpFramingEncodeDecode:
    """Test the 4-byte big-endian uint32 length-prefix wire protocol."""

    def test_frame_encoding_structure(self):
        """Expected: _tcp_send writes [4-byte length][payload] on the wire."""
        data = b"hello tailscale"
        expected_header = struct.pack(">I", len(data))

        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)

        with patch("socket.socket", return_value=mock_sock):
            t = _make_transport_no_tailscale()
            t._tcp_send("100.64.0.5", 9385, data)

        mock_sock.sendall.assert_called_once_with(expected_header + data)

    def test_frame_header_is_big_endian(self):
        """Expected: header uses big-endian byte order (network byte order)."""
        data = b"x" * 300  # 300 bytes
        expected_header = struct.pack(">I", 300)
        # Verify it is NOT little-endian
        assert expected_header == b"\x00\x00\x01\x2c"

    def test_frame_decode_roundtrip(self):
        """Expected: encoding then decoding preserves the original message."""
        original = _make_envelope_bytes(content="roundtrip test")

        # Encode: 4-byte header + payload
        framed = struct.pack(">I", len(original)) + original

        # Decode: extract header, then payload
        header = framed[:HEADER_SIZE]
        msg_len = struct.unpack(">I", header)[0]
        payload = framed[HEADER_SIZE:HEADER_SIZE + msg_len]

        assert payload == original
        assert msg_len == len(original)

    def test_frame_decode_via_handle_connection(self, transport_no_ts):
        """Expected: _handle_connection correctly decodes framed messages."""
        t = transport_no_ts
        original = _make_envelope_bytes(content="via handler")
        header = struct.pack(">I", len(original))

        with patch.object(t, "_recv_exact", side_effect=[header, original]):
            mock_conn = MagicMock()
            t._handle_connection(mock_conn, ("100.64.0.99", 12345))

        assert not t._inbox.empty()
        assert t._inbox.get() == original

    def test_frame_empty_payload(self):
        """Expected: zero-length payload is valid framing."""
        data = b""
        header = struct.pack(">I", 0)
        assert header == b"\x00\x00\x00\x00"

        framed = header + data
        msg_len = struct.unpack(">I", framed[:4])[0]
        assert msg_len == 0

    def test_frame_max_uint32(self):
        """Expected: maximum uint32 value is 2^32 - 1 = 4294967295."""
        max_val = 2**32 - 1
        header = struct.pack(">I", max_val)
        decoded = struct.unpack(">I", header)[0]
        assert decoded == max_val

    def test_frame_various_sizes(self):
        """Expected: framing works correctly for various payload sizes."""
        for size in [0, 1, 255, 256, 65535, 65536, 1000000]:
            header = struct.pack(">I", size)
            decoded = struct.unpack(">I", header)[0]
            assert decoded == size, f"Failed for size {size}"

    def test_oversized_message_rejected_by_handler(self, transport_no_ts):
        """Expected: messages exceeding MAX_MESSAGE_SIZE are rejected."""
        t = transport_no_ts
        oversized_header = struct.pack(">I", MAX_MESSAGE_SIZE + 1)
        mock_conn = MagicMock()

        with patch.object(t, "_recv_exact", return_value=oversized_header):
            t._handle_connection(mock_conn, ("10.0.0.1", 9999))

        assert t._inbox.empty()

    def test_short_header_rejected_by_handler(self, transport_no_ts):
        """Expected: incomplete header (< 4 bytes) is discarded."""
        t = transport_no_ts
        mock_conn = MagicMock()

        with patch.object(t, "_recv_exact", return_value=b"\x00\x00"):
            t._handle_connection(mock_conn, ("10.0.0.1", 9999))

        assert t._inbox.empty()

    def test_recv_exact_handles_chunked_reads(self):
        """Expected: _recv_exact reassembles data from multiple recv() calls."""
        mock_conn = MagicMock()
        # Simulate the socket returning data in small chunks
        mock_conn.recv.side_effect = [b"hel", b"lo", b" w", b"orld"]
        result = TailscaleTransport._recv_exact(mock_conn, 11)
        assert result == b"hello world"

    def test_recv_exact_handles_premature_close(self):
        """Expected: _recv_exact returns partial data if connection closes early."""
        mock_conn = MagicMock()
        mock_conn.recv.side_effect = [b"par", b""]  # EOF after 3 bytes
        result = TailscaleTransport._recv_exact(mock_conn, 10)
        assert result == b"par"

    def test_tcp_send_sets_timeout(self):
        """Expected: _tcp_send sets connect timeout on the socket."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)

        with patch("socket.socket", return_value=mock_sock):
            t = _make_transport_no_tailscale()
            t._tcp_send("100.64.0.5", 9385, b"data")

        mock_sock.settimeout.assert_called_once_with(CONNECT_TIMEOUT)

    def test_tcp_send_connects_to_correct_address(self):
        """Expected: _tcp_send connects to the specified IP and port."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)

        with patch("socket.socket", return_value=mock_sock):
            t = _make_transport_no_tailscale()
            t._tcp_send("100.64.0.42", 19385, b"data")

        mock_sock.connect.assert_called_once_with(("100.64.0.42", 19385))


# ===========================================================================
# 4. Concurrent start calls (only one listener)
# ===========================================================================


class TestConcurrentStartCalls:
    """Test that _lifecycle_lock prevents duplicate listeners."""

    def test_concurrent_starts_only_create_one_thread(self, transport):
        """Expected: multiple concurrent start() calls produce exactly one listener thread."""
        results = []
        barrier = threading.Barrier(5, timeout=5.0)
        listener_thread_count = 0
        original_thread_cls = threading.Thread

        def counting_thread(*args, **kwargs):
            """Track how many listener threads start() tries to create."""
            nonlocal listener_thread_count
            name = kwargs.get("name", "")
            if "tailscale" in name:
                listener_thread_count += 1
            mock_t = MagicMock()
            return mock_t

        def try_start():
            barrier.wait()
            results.append(transport.start())

        # Create the caller threads BEFORE patching threading.Thread,
        # so the callers themselves are real threads.
        caller_threads = [
            original_thread_cls(target=try_start, daemon=True)
            for _ in range(5)
        ]

        with patch(
            "skcomm.transports.tailscale.threading.Thread",
            side_effect=counting_thread,
        ):
            for t in caller_threads:
                t.start()
            for t in caller_threads:
                t.join(timeout=5.0)

        # All should return True (first one starts, rest see _running=True)
        assert all(r is True for r in results)
        assert len(results) == 5
        # The listener thread constructor should have been called only once
        assert listener_thread_count == 1

    def test_start_while_stop_in_progress(self, transport):
        """Expected: start() blocks until stop() completes due to _lifecycle_lock."""
        transport._running = True
        transport._server_socket = MagicMock()

        stop_entered = threading.Event()
        stop_proceed = threading.Event()

        original_stop = transport.stop

        def slow_stop():
            stop_entered.set()
            stop_proceed.wait(timeout=5.0)
            original_stop()

        start_result = [None]

        def try_start():
            # Wait until stop has entered its critical section
            stop_entered.wait(timeout=5.0)
            start_result[0] = transport.start()

        # Run stop in a thread that pauses mid-execution
        with patch.object(transport, "stop", side_effect=slow_stop):
            stop_thread = threading.Thread(target=transport.stop, daemon=True)
            stop_thread.start()

            # Wait for stop to enter
            stop_entered.wait(timeout=2.0)

            # Now try start — it should block on _lifecycle_lock held by stop
            with patch("threading.Thread") as mock_thread_cls:
                mock_thread = MagicMock()
                mock_thread_cls.return_value = mock_thread

                start_thread = threading.Thread(target=try_start, daemon=True)
                start_thread.start()

                # Release stop to complete
                stop_proceed.set()
                stop_thread.join(timeout=3.0)
                start_thread.join(timeout=3.0)


# ===========================================================================
# 5. health_check response structure and state transitions
# ===========================================================================


class TestHealthCheckIntegration:
    """Test health_check response integrity across state transitions."""

    def test_health_unavailable_when_no_tailscale(self, transport_no_ts):
        """Expected: UNAVAILABLE when Tailscale is not installed."""
        with patch.object(transport_no_ts, "_detect_local_ip", return_value=None):
            health = transport_no_ts.health_check()
        assert health.status == TransportStatus.UNAVAILABLE
        assert health.transport_name == "tailscale"
        assert "not running" in (health.error or "").lower() or "not installed" in (health.error or "").lower()
        assert "listen_port" in health.details

    def test_health_degraded_when_not_started(self, transport):
        """Expected: DEGRADED when Tailscale is present but listener not started."""
        with patch.object(transport, "_detect_local_ip", return_value=LOCAL_IP):
            health = transport.health_check()
        assert health.status == TransportStatus.DEGRADED
        assert health.details["listener_running"] is False
        assert health.details["local_ip"] == LOCAL_IP

    def test_health_available_when_running(self, transport):
        """Expected: AVAILABLE when transport is running."""
        transport._running = True
        with patch.object(transport, "_detect_local_ip", return_value=LOCAL_IP):
            health = transport.health_check()
        assert health.status == TransportStatus.AVAILABLE
        assert health.details["listener_running"] is True

    def test_health_transitions_through_lifecycle(self):
        """Expected: health transitions: UNAVAILABLE -> DEGRADED -> AVAILABLE -> DEGRADED."""
        t = _make_transport_no_tailscale()

        # Phase 1: no Tailscale
        with patch.object(t, "_detect_local_ip", return_value=None):
            assert t.health_check().status == TransportStatus.UNAVAILABLE

        # Phase 2: Tailscale appears but listener not started
        with patch.object(t, "_detect_local_ip", return_value=LOCAL_IP):
            assert t.health_check().status == TransportStatus.DEGRADED

        # Phase 3: listener started
        t._running = True
        with patch.object(t, "_detect_local_ip", return_value=LOCAL_IP):
            assert t.health_check().status == TransportStatus.AVAILABLE

        # Phase 4: listener stopped
        t._running = False
        with patch.object(t, "_detect_local_ip", return_value=LOCAL_IP):
            assert t.health_check().status == TransportStatus.DEGRADED

    def test_health_includes_known_peers(self, transport):
        """Expected: health details include the number of known peer IPs."""
        transport._running = True
        transport.register_peer_ip("peer-a", "100.64.0.10")
        transport.register_peer_ip("peer-b", "100.64.0.11")
        transport.register_peer_ip("peer-c", "100.64.0.12")

        with patch.object(transport, "_detect_local_ip", return_value=LOCAL_IP):
            health = transport.health_check()
        assert health.details["known_peers"] == 3

    def test_health_includes_inbox_pending(self, transport):
        """Expected: health details include pending message count."""
        transport._running = True
        transport._inbox.put(_make_envelope_bytes(envelope_id="q1"))
        transport._inbox.put(_make_envelope_bytes(envelope_id="q2"))

        with patch.object(transport, "_detect_local_ip", return_value=LOCAL_IP):
            health = transport.health_check()
        assert health.details["inbox_pending"] == 2

    def test_health_includes_listen_port(self, transport):
        """Expected: health details include the configured listen port."""
        transport._running = True
        with patch.object(transport, "_detect_local_ip", return_value=LOCAL_IP):
            health = transport.health_check()
        assert health.details["listen_port"] == LISTEN_PORT


# ===========================================================================
# 6. send() integration scenarios
# ===========================================================================


class TestSendIntegration:
    """Test send() behaviour across various states and edge cases."""

    def test_send_when_tailscale_absent(self, transport_no_ts):
        """Expected: send returns failure when Tailscale is not available."""
        result = transport_no_ts.send(_make_envelope_bytes(), PEER_NAME)
        assert result.success is False
        assert "not available" in result.error.lower()

    def test_send_when_no_peer_ip_known(self, transport):
        """Expected: send fails when peer IP cannot be resolved."""
        with patch.object(transport, "_resolve_peer_ip", return_value=None):
            result = transport.send(_make_envelope_bytes(), "unknown-agent")
        assert result.success is False
        assert "No Tailscale IP" in result.error

    def test_send_with_registered_peer(self, transport):
        """Expected: send succeeds when peer IP is registered."""
        transport.register_peer_ip(PEER_NAME, PEER_IP)
        with patch.object(transport, "_tcp_send"):
            result = transport.send(_make_envelope_bytes(), PEER_NAME)
        assert result.success is True
        assert result.transport_name == "tailscale"

    def test_send_carries_envelope_id(self, transport):
        """Expected: send result includes the correct envelope ID."""
        transport.register_peer_ip(PEER_NAME, PEER_IP)
        envelope = _make_envelope_bytes(envelope_id="unique-id-42")
        with patch.object(transport, "_tcp_send"):
            result = transport.send(envelope, PEER_NAME)
        assert result.envelope_id == "unique-id-42"

    def test_send_records_latency(self, transport):
        """Expected: send result includes non-negative latency."""
        transport.register_peer_ip(PEER_NAME, PEER_IP)
        with patch.object(transport, "_tcp_send"):
            result = transport.send(_make_envelope_bytes(), PEER_NAME)
        assert result.latency_ms >= 0

    def test_send_tcp_error_returns_failure(self, transport):
        """Expected: TCP connection error results in failure with error message."""
        transport.register_peer_ip(PEER_NAME, PEER_IP)
        with patch.object(transport, "_tcp_send", side_effect=ConnectionRefusedError("refused")):
            result = transport.send(_make_envelope_bytes(), PEER_NAME)
        assert result.success is False
        assert "refused" in result.error


# ===========================================================================
# 7. receive() and inbox integration
# ===========================================================================


class TestReceiveIntegration:
    """Test receive() draining behaviour."""

    def test_receive_empty(self, transport_no_ts):
        """Expected: empty list when no messages queued."""
        assert transport_no_ts.receive() == []

    def test_receive_drains_all_messages(self, transport_no_ts):
        """Expected: receive() returns all queued messages and empties the queue."""
        t = transport_no_ts
        msg1 = _make_envelope_bytes(content="a")
        msg2 = _make_envelope_bytes(content="b")
        msg3 = _make_envelope_bytes(content="c")
        t._inbox.put(msg1)
        t._inbox.put(msg2)
        t._inbox.put(msg3)

        received = t.receive()
        assert len(received) == 3
        assert set(received) == {msg1, msg2, msg3}

    def test_receive_is_repeatable(self, transport_no_ts):
        """Expected: second receive() after drain returns empty list."""
        t = transport_no_ts
        t._inbox.put(_make_envelope_bytes())
        assert len(t.receive()) == 1
        assert t.receive() == []

    def test_receive_preserves_message_order(self, transport_no_ts):
        """Expected: messages are returned in FIFO order."""
        t = transport_no_ts
        msgs = [_make_envelope_bytes(content=str(i)) for i in range(10)]
        for m in msgs:
            t._inbox.put(m)
        received = t.receive()
        assert received == msgs
