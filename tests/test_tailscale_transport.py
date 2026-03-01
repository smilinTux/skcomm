"""Tests for the Tailscale TCP transport."""

from __future__ import annotations

import json
import socket
import struct
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

pytest.importorskip("skcomm.transports.tailscale")

from skcomm.models import MessageEnvelope, MessagePayload
from skcomm.transport import TransportCategory, TransportStatus
from skcomm.transports.tailscale import (
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


def make_envelope(sender="opus", recipient="lumina", content="Hello Tailscale"):
    return MessageEnvelope(
        sender=sender,
        recipient=recipient,
        payload=MessagePayload(content=content),
    ).to_bytes()


def _length_prefix(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport_no_tailscale():
    """Transport where Tailscale is absent (no local IP)."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        t = TailscaleTransport(auto_detect=False)
    return t


@pytest.fixture
def transport_with_tailscale():
    """Transport where Tailscale is running with a valid local IP."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = LOCAL_IP + "\n"
    with patch("subprocess.run", return_value=mock_result):
        t = TailscaleTransport(auto_detect=False)
    return t


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestTailscaleTransportInit:
    """Tests for transport construction."""

    def test_default_name_and_category(self, transport_with_tailscale):
        """Expected: name is 'tailscale', category is REALTIME."""
        t = transport_with_tailscale
        assert t.name == "tailscale"
        assert t.category == TransportCategory.REALTIME

    def test_default_priority(self, transport_with_tailscale):
        """Expected: default priority is 2."""
        assert transport_with_tailscale.priority == 2

    def test_custom_listen_port(self):
        """Expected: custom listen_port is respected."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = TailscaleTransport(listen_port=12345, auto_detect=False)
        assert t._listen_port == 12345

    def test_custom_priority(self):
        """Expected: custom priority override is stored."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = TailscaleTransport(priority=5, auto_detect=False)
        assert t.priority == 5

    def test_not_running_on_init(self, transport_no_tailscale):
        """Expected: transport is not running after construction."""
        assert transport_no_tailscale._running is False

    def test_local_ip_detected_on_init(self, transport_with_tailscale):
        """Expected: _local_ip is set when Tailscale is available."""
        assert transport_with_tailscale._local_ip == LOCAL_IP

    def test_local_ip_none_when_tailscale_absent(self, transport_no_tailscale):
        """Expected: _local_ip is None when Tailscale binary is missing."""
        assert transport_no_tailscale._local_ip is None


# ---------------------------------------------------------------------------
# configure()
# ---------------------------------------------------------------------------


class TestTailscaleTransportConfigure:
    """Tests for configure()."""

    def test_configure_listen_port(self, transport_no_tailscale):
        """Expected: configure() updates the listen port."""
        transport_no_tailscale.configure({"listen_port": 9999})
        assert transport_no_tailscale._listen_port == 9999

    def test_configure_priority(self, transport_no_tailscale):
        """Expected: configure() updates priority."""
        transport_no_tailscale.configure({"priority": 3})
        assert transport_no_tailscale.priority == 3

    def test_configure_auto_detect(self, transport_no_tailscale):
        """Expected: configure() updates auto_detect flag."""
        transport_no_tailscale.configure({"auto_detect": True})
        assert transport_no_tailscale._auto_detect is True


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestTailscaleTransportAvailability:
    """Tests for is_available()."""

    def test_available_when_local_ip_set(self, transport_with_tailscale):
        """Expected: True when _local_ip is set."""
        assert transport_with_tailscale.is_available() is True

    def test_not_available_when_no_local_ip(self, transport_no_tailscale):
        """Expected: False when tailscale is absent."""
        assert transport_no_tailscale.is_available() is False

    def test_re_detects_local_ip_on_check(self):
        """Expected: is_available() re-runs detection if _local_ip is None."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = LOCAL_IP + "\n"
        with patch("subprocess.run", return_value=mock_result):
            t = TailscaleTransport(auto_detect=False)
        t._local_ip = None  # simulate loss of IP

        mock_result2 = MagicMock()
        mock_result2.returncode = 0
        mock_result2.stdout = LOCAL_IP + "\n"
        with patch("subprocess.run", return_value=mock_result2):
            result = t.is_available()

        assert result is True


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


class TestTailscaleTransportSend:
    """Tests for send()."""

    def test_send_fails_when_tailscale_absent(self, transport_no_tailscale):
        """Expected: send returns failure when Tailscale is not available."""
        data = make_envelope()
        result = transport_no_tailscale.send(data, PEER_NAME)
        assert result.success is False
        assert "not available" in result.error.lower()

    def test_send_fails_when_no_peer_ip(self, transport_with_tailscale):
        """Expected: send returns failure when peer IP is unknown."""
        t = transport_with_tailscale
        # Peer store and auto-detect both return nothing
        with patch.object(t, "_resolve_peer_ip", return_value=None):
            result = t.send(make_envelope(), "unknown-peer")
        assert result.success is False
        assert "No Tailscale IP" in result.error

    def test_send_succeeds_with_known_ip(self, transport_with_tailscale):
        """Expected: send calls _tcp_send and returns success."""
        t = transport_with_tailscale
        t.register_peer_ip(PEER_NAME, PEER_IP)
        with patch.object(t, "_tcp_send") as mock_tcp:
            envelope_bytes = make_envelope()
            result = t.send(envelope_bytes, PEER_NAME)
        assert result.success is True
        assert result.transport_name == "tailscale"
        # Verify _tcp_send was called with correct IP and port
        mock_tcp.assert_called_once()
        call_args = mock_tcp.call_args[0]
        assert call_args[0] == PEER_IP
        assert call_args[1] == t._listen_port
        assert call_args[2] == envelope_bytes

    def test_send_records_latency(self, transport_with_tailscale):
        """Expected: send result includes non-negative latency_ms."""
        t = transport_with_tailscale
        t.register_peer_ip(PEER_NAME, PEER_IP)
        with patch.object(t, "_tcp_send"):
            result = t.send(make_envelope(), PEER_NAME)
        assert result.latency_ms >= 0

    def test_send_failure_on_tcp_exception(self, transport_with_tailscale):
        """Expected: send returns failure when TCP raises."""
        t = transport_with_tailscale
        t.register_peer_ip(PEER_NAME, PEER_IP)
        with patch.object(t, "_tcp_send", side_effect=ConnectionRefusedError("refused")):
            result = t.send(make_envelope(), PEER_NAME)
        assert result.success is False
        assert "refused" in result.error

    def test_send_includes_envelope_id(self, transport_with_tailscale):
        """Expected: send result contains the envelope's ID."""
        t = transport_with_tailscale
        t.register_peer_ip(PEER_NAME, PEER_IP)
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="ts test"),
        )
        with patch.object(t, "_tcp_send"):
            result = t.send(env.to_bytes(), PEER_NAME)
        assert result.envelope_id == env.envelope_id


# ---------------------------------------------------------------------------
# receive()
# ---------------------------------------------------------------------------


class TestTailscaleTransportReceive:
    """Tests for receive()."""

    def test_receive_empty_inbox(self, transport_no_tailscale):
        """Expected: empty list when no messages queued."""
        assert transport_no_tailscale.receive() == []

    def test_receive_drains_queue(self, transport_no_tailscale):
        """Expected: all queued messages returned and queue cleared."""
        t = transport_no_tailscale
        env1, env2 = make_envelope(content="a"), make_envelope(content="b")
        t._inbox.put(env1)
        t._inbox.put(env2)

        received = t.receive()
        assert set(received) == {env1, env2}
        assert t.receive() == []


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


class TestTailscaleTransportHealthCheck:
    """Tests for health_check()."""

    def test_unavailable_when_no_local_ip(self, transport_no_tailscale):
        """Expected: UNAVAILABLE when Tailscale is absent."""
        with patch.object(transport_no_tailscale, "_detect_local_ip", return_value=None):
            health = transport_no_tailscale.health_check()
        assert health.status == TransportStatus.UNAVAILABLE

    def test_degraded_when_available_but_not_running(self, transport_with_tailscale):
        """Expected: DEGRADED when Tailscale is present but listener not started."""
        with patch.object(transport_with_tailscale, "_detect_local_ip", return_value=LOCAL_IP):
            health = transport_with_tailscale.health_check()
        assert health.status == TransportStatus.DEGRADED
        assert health.details["listener_running"] is False

    def test_available_when_running(self, transport_with_tailscale):
        """Expected: AVAILABLE when running flag is True."""
        t = transport_with_tailscale
        t._running = True  # simulate started (without actual socket)
        with patch.object(t, "_detect_local_ip", return_value=LOCAL_IP):
            health = t.health_check()
        assert health.status == TransportStatus.AVAILABLE

    def test_health_includes_local_ip(self, transport_with_tailscale):
        """Expected: health details include the local Tailscale IP."""
        t = transport_with_tailscale
        t._running = True
        with patch.object(t, "_detect_local_ip", return_value=LOCAL_IP):
            health = t.health_check()
        assert health.details["local_ip"] == LOCAL_IP

    def test_health_includes_known_peer_count(self, transport_with_tailscale):
        """Expected: health details include known peer count."""
        t = transport_with_tailscale
        t._running = True
        t.register_peer_ip("peer1", "100.64.0.5")
        t.register_peer_ip("peer2", "100.64.0.6")
        with patch.object(t, "_detect_local_ip", return_value=LOCAL_IP):
            health = t.health_check()
        assert health.details["known_peers"] == 2

    def test_health_reports_inbox_pending(self, transport_with_tailscale):
        """Expected: health details include pending inbox count."""
        t = transport_with_tailscale
        t._running = True
        t._inbox.put(make_envelope())
        with patch.object(t, "_detect_local_ip", return_value=LOCAL_IP):
            health = t.health_check()
        assert health.details["inbox_pending"] == 1


# ---------------------------------------------------------------------------
# register_peer_ip() and _resolve_peer_ip()
# ---------------------------------------------------------------------------


class TestPeerIpResolution:
    """Tests for peer IP registration and resolution."""

    def test_register_peer_ip(self, transport_no_tailscale):
        """Expected: registered IP returned by _resolve_peer_ip."""
        t = transport_no_tailscale
        t.register_peer_ip("jarvis", "100.99.0.1")
        assert t._resolve_peer_ip("jarvis") == "100.99.0.1"

    def test_manual_registry_takes_precedence(self, transport_no_tailscale):
        """Expected: manual registry is checked before peer store."""
        t = transport_no_tailscale
        t.register_peer_ip("agent", "100.1.2.3")
        with patch.object(t, "_peer_ip_from_store", return_value="100.9.9.9") as mock_store:
            result = t._resolve_peer_ip("agent")
        mock_store.assert_not_called()
        assert result == "100.1.2.3"

    def test_peer_store_fallback(self, transport_no_tailscale):
        """Expected: peer store is tried when manual registry has no entry."""
        t = transport_no_tailscale
        with patch.object(t, "_peer_ip_from_store", return_value="100.5.6.7"):
            result = t._resolve_peer_ip("unknown-peer")
        assert result == "100.5.6.7"

    def test_tailscale_status_fallback(self, transport_no_tailscale):
        """Expected: tailscale status lookup is tried when store returns None."""
        t = transport_no_tailscale
        t._auto_detect = True
        with patch.object(t, "_peer_ip_from_store", return_value=None):
            with patch.object(t, "_peer_ip_from_tailscale_status", return_value="100.7.8.9"):
                result = t._resolve_peer_ip("new-peer")
        assert result == "100.7.8.9"

    def test_no_ip_found_returns_none(self, transport_no_tailscale):
        """Expected: None returned when all sources fail."""
        t = transport_no_tailscale
        t._auto_detect = False
        with patch.object(t, "_peer_ip_from_store", return_value=None):
            result = t._resolve_peer_ip("ghost")
        assert result is None

    def test_auto_detect_disabled_skips_tailscale_status(self, transport_no_tailscale):
        """Expected: tailscale status is not queried when auto_detect=False."""
        t = transport_no_tailscale
        t._auto_detect = False
        with patch.object(t, "_peer_ip_from_store", return_value=None):
            with patch.object(t, "_peer_ip_from_tailscale_status") as mock_status:
                result = t._resolve_peer_ip("ghost")
        mock_status.assert_not_called()
        assert result is None


# ---------------------------------------------------------------------------
# _peer_ip_from_tailscale_status()
# ---------------------------------------------------------------------------


class TestTailscaleStatusLookup:
    """Tests for the tailscale status JSON lookup."""

    def test_hostname_match(self, transport_no_tailscale):
        """Expected: returns IP when HostName matches recipient."""
        t = transport_no_tailscale
        status_json = json.dumps({
            "Peer": {
                "nodekey:abc": {
                    "HostName": "lumina-host",
                    "DNSName": "lumina.tailnet",
                    "TailscaleIPs": ["100.64.0.50", "fd7a::1"],
                }
            }
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = status_json
        with patch("subprocess.run", return_value=mock_result):
            ip = t._peer_ip_from_tailscale_status("lumina")
        assert ip == "100.64.0.50"

    def test_dns_name_match(self, transport_no_tailscale):
        """Expected: returns IP when DNSName matches."""
        t = transport_no_tailscale
        status_json = json.dumps({
            "Peer": {
                "nodekey:xyz": {
                    "HostName": "some-host",
                    "DNSName": "opus.tailnet",
                    "TailscaleIPs": ["100.64.0.99"],
                }
            }
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = status_json
        with patch("subprocess.run", return_value=mock_result):
            ip = t._peer_ip_from_tailscale_status("opus")
        assert ip == "100.64.0.99"

    def test_no_match_returns_none(self, transport_no_tailscale):
        """Expected: None when no peer hostname matches."""
        t = transport_no_tailscale
        status_json = json.dumps({"Peer": {}})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = status_json
        with patch("subprocess.run", return_value=mock_result):
            ip = t._peer_ip_from_tailscale_status("ghost")
        assert ip is None

    def test_subprocess_failure_returns_none(self, transport_no_tailscale):
        """Expected: None when tailscale command fails."""
        t = transport_no_tailscale
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            ip = t._peer_ip_from_tailscale_status("lumina")
        assert ip is None

    def test_subprocess_exception_returns_none(self, transport_no_tailscale):
        """Expected: None when tailscale subprocess raises."""
        t = transport_no_tailscale
        with patch("subprocess.run", side_effect=FileNotFoundError("no tailscale")):
            ip = t._peer_ip_from_tailscale_status("lumina")
        assert ip is None

    def test_skips_non_100_ips(self, transport_no_tailscale):
        """Expected: only 100.x.x.x IPs are returned (not IPv6)."""
        t = transport_no_tailscale
        status_json = json.dumps({
            "Peer": {
                "nodekey:foo": {
                    "HostName": "target",
                    "DNSName": "target.net",
                    "TailscaleIPs": ["fd7a::1:2", "100.1.2.3"],
                }
            }
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = status_json
        with patch("subprocess.run", return_value=mock_result):
            ip = t._peer_ip_from_tailscale_status("target")
        assert ip == "100.1.2.3"


# ---------------------------------------------------------------------------
# _detect_local_ip()
# ---------------------------------------------------------------------------


class TestDetectLocalIp:
    """Tests for the local IP detection method."""

    def test_detects_valid_ip(self):
        """Expected: returns the 100.x.x.x IP from tailscale ip -4."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "100.64.0.1\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            with patch("subprocess.run", return_value=mock_result):
                t = TailscaleTransport(auto_detect=False)
                ip = t._detect_local_ip()
        assert ip == "100.64.0.1"

    def test_returns_none_when_tailscale_not_found(self):
        """Expected: None when tailscale binary is absent."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = TailscaleTransport(auto_detect=False)
        assert t._local_ip is None

    def test_returns_none_on_nonzero_returncode(self):
        """Expected: None when tailscale ip -4 exits non-zero."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            t = TailscaleTransport(auto_detect=False)
        assert t._local_ip is None

    def test_returns_none_for_non_100_ip(self):
        """Expected: None when returned IP is not in 100.x.x.x range."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "192.168.1.1\n"
        with patch("subprocess.run", return_value=mock_result):
            t = TailscaleTransport(auto_detect=False)
        assert t._local_ip is None


# ---------------------------------------------------------------------------
# _tcp_send()
# ---------------------------------------------------------------------------


class TestTcpSend:
    """Tests for the TCP send method."""

    def test_tcp_send_uses_length_prefix(self):
        """Expected: sends 4-byte big-endian length prefix followed by data."""
        data = b"hello tailscale envelope"
        expected_header = struct.pack(">I", len(data))

        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)

        with patch("socket.socket", return_value=mock_sock):
            t = TailscaleTransport(auto_detect=False)
            with patch("subprocess.run", side_effect=FileNotFoundError):
                t._tcp_send("100.64.0.5", 9385, data)

        mock_sock.sendall.assert_called_once_with(expected_header + data)

    def test_tcp_send_sets_connect_timeout(self):
        """Expected: socket timeout is set before connect."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)

        with patch("socket.socket", return_value=mock_sock):
            t = TailscaleTransport(auto_detect=False)
            with patch("subprocess.run", side_effect=FileNotFoundError):
                t._tcp_send("100.64.0.5", 9385, b"data")

        mock_sock.settimeout.assert_called_once_with(CONNECT_TIMEOUT)


# ---------------------------------------------------------------------------
# _recv_exact()
# ---------------------------------------------------------------------------


class TestRecvExact:
    """Tests for the exact-read helper."""

    def test_reads_exact_bytes(self):
        """Expected: returns exactly n bytes from multiple recv chunks."""
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = [b"hel", b"lo"]

        result = TailscaleTransport._recv_exact(mock_sock, 5)
        assert result == b"hello"

    def test_handles_short_read_on_close(self):
        """Expected: returns partial data when connection closes mid-read."""
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = [b"hi", b""]  # EOF after 2 bytes

        result = TailscaleTransport._recv_exact(mock_sock, 10)
        assert result == b"hi"


# ---------------------------------------------------------------------------
# _handle_connection() (inbound TCP)
# ---------------------------------------------------------------------------


class TestHandleConnection:
    """Tests for the inbound connection handler."""

    def test_valid_message_added_to_inbox(self, transport_no_tailscale):
        """Expected: valid length-prefixed message is buffered in inbox."""
        t = transport_no_tailscale
        data = make_envelope()
        framed = _length_prefix(data)

        # Simulate recv: header bytes then payload bytes
        mock_conn = MagicMock()
        mock_conn.recv.side_effect = [
            framed[:HEADER_SIZE],       # length header
            framed[HEADER_SIZE:],       # payload
            b"",
        ]

        with patch.object(t, "_recv_exact", side_effect=[
            framed[:HEADER_SIZE],
            data,
        ]):
            t._handle_connection(mock_conn, ("100.64.0.99", 12345))

        assert not t._inbox.empty()
        assert t._inbox.get() == data

    def test_short_header_discarded(self, transport_no_tailscale):
        """Expected: message with short header is silently dropped."""
        t = transport_no_tailscale
        mock_conn = MagicMock()
        with patch.object(t, "_recv_exact", return_value=b"\x00\x00"):  # only 2 bytes
            t._handle_connection(mock_conn, ("10.0.0.1", 9999))
        assert t._inbox.empty()

    def test_oversized_message_rejected(self, transport_no_tailscale):
        """Expected: message exceeding MAX_MESSAGE_SIZE is rejected."""
        t = transport_no_tailscale
        oversized_header = struct.pack(">I", MAX_MESSAGE_SIZE + 1)
        mock_conn = MagicMock()
        with patch.object(t, "_recv_exact", return_value=oversized_header):
            t._handle_connection(mock_conn, ("10.0.0.1", 9999))
        assert t._inbox.empty()


# ---------------------------------------------------------------------------
# _extract_id() helper
# ---------------------------------------------------------------------------


class TestExtractId:
    """Tests for the envelope ID extractor."""

    def test_extracts_valid_envelope_id(self):
        """Expected: returns envelope_id from valid JSON."""
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="test"),
        )
        eid = TailscaleTransport._extract_id(env.to_bytes())
        assert eid == env.envelope_id

    def test_fallback_on_garbage(self):
        """Expected: returns unknown-<ts> for non-JSON bytes."""
        eid = TailscaleTransport._extract_id(b"\xff\xfe not json")
        assert eid.startswith("unknown-")

    def test_fallback_when_key_absent(self):
        """Expected: returns unknown-<ts> when envelope_id key is missing."""
        data = json.dumps({"other": "field"}).encode()
        eid = TailscaleTransport._extract_id(data)
        assert eid.startswith("unknown-")


# ---------------------------------------------------------------------------
# create_transport() factory
# ---------------------------------------------------------------------------


class TestCreateTransportFactory:
    """Tests for the create_transport factory function."""

    def test_factory_returns_tailscale_transport(self):
        """Expected: factory returns a TailscaleTransport instance."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = create_transport()
        assert isinstance(t, TailscaleTransport)

    def test_factory_default_port(self):
        """Expected: factory uses LISTEN_PORT by default."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = create_transport()
        assert t._listen_port == LISTEN_PORT

    def test_factory_custom_port(self):
        """Expected: factory passes custom listen_port through."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = create_transport(listen_port=19000)
        assert t._listen_port == 19000

    def test_factory_custom_priority(self):
        """Expected: factory passes priority through."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = create_transport(priority=4)
        assert t.priority == 4

    def test_factory_not_running(self):
        """Expected: factory does not start the listener by default."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            t = create_transport()
        assert t._running is False
