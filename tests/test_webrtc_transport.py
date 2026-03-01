"""Tests for the WebRTC transport (WebRTCTransport)."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("aiortc")

from skcomm.models import MessageEnvelope, MessagePayload
from skcomm.transport import TransportCategory, TransportStatus
from skcomm.transports.webrtc import (
    CONNECT_SETTLE,
    DEFAULT_SIGNALING_URL,
    ICE_GATHER_TIMEOUT,
    PeerConnection,
    SEND_TIMEOUT,
    WebRTCTransport,
    create_transport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PEER_FP = "AAAA9306410CF8CD5E393D6DEC31663B95230684"
LOCAL_FP = "CCBE9306410CF8CD5E393D6DEC31663B95230684"


def make_envelope(sender="opus", recipient="lumina", content="Hello WebRTC"):
    return MessageEnvelope(
        sender=sender,
        recipient=recipient,
        payload=MessagePayload(content=content),
    ).to_bytes()


def _make_transport(**kwargs) -> WebRTCTransport:
    """Build a transport without starting the background thread."""
    return WebRTCTransport(
        agent_fingerprint=LOCAL_FP,
        agent_name="opus",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport():
    """A WebRTCTransport that is NOT started."""
    return _make_transport()


@pytest.fixture
def running_transport():
    """A WebRTCTransport with _running=True and a mocked event loop."""
    t = _make_transport()
    t._running = True
    t._loop = MagicMock(spec=asyncio.AbstractEventLoop)
    t._loop.is_closed.return_value = False
    return t


@pytest.fixture
def connected_transport():
    """A WebRTCTransport with signaling connected."""
    t = _make_transport()
    t._running = True
    t._signaling_connected = True
    t._loop = MagicMock(spec=asyncio.AbstractEventLoop)
    t._loop.is_closed.return_value = False
    return t


# ---------------------------------------------------------------------------
# PeerConnection dataclass
# ---------------------------------------------------------------------------


class TestPeerConnection:
    """Tests for the PeerConnection dataclass."""

    def test_defaults(self):
        """Expected: PeerConnection defaults are correct."""
        pc = PeerConnection(peer_fingerprint=PEER_FP, pc=None)
        assert pc.connected is False
        assert pc.negotiating is False
        assert pc.channel is None
        assert pc.pending == []


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestWebRTCTransportInit:
    """Tests for transport construction."""

    def test_default_name_and_category(self, transport):
        """Expected: name is 'webrtc', category is REALTIME."""
        assert transport.name == "webrtc"
        assert transport.category == TransportCategory.REALTIME

    def test_default_priority(self, transport):
        """Expected: default priority is 1 (highest)."""
        assert transport.priority == 1

    def test_default_signaling_url(self, transport):
        """Expected: default signaling URL is set."""
        assert transport._signaling_url == DEFAULT_SIGNALING_URL

    def test_not_running_on_init(self, transport):
        """Expected: transport not running after construction."""
        assert transport._running is False

    def test_not_signaling_connected_on_init(self, transport):
        """Expected: signaling not connected after construction."""
        assert transport._signaling_connected is False

    def test_custom_signaling_url(self):
        """Expected: custom signaling URL stored."""
        t = WebRTCTransport(signaling_url="wss://signal.example.com/ws")
        assert t._signaling_url == "wss://signal.example.com/ws"

    def test_custom_token(self):
        """Expected: CapAuth token stored."""
        t = WebRTCTransport(token="my-capauth-token")
        assert t._token == "my-capauth-token"

    def test_custom_stun_servers(self):
        """Expected: custom STUN server list stored."""
        stun = ["stun:custom.stun.io:3478"]
        t = WebRTCTransport(stun_servers=stun)
        assert t._stun_servers == stun

    def test_custom_turn_secret(self):
        """Expected: TURN secret stored."""
        t = WebRTCTransport(turn_secret="mysecret")
        assert t._turn_secret == "mysecret"

    def test_custom_priority(self):
        """Expected: priority override stored."""
        t = WebRTCTransport(priority=3)
        assert t.priority == 3

    def test_empty_peers_on_init(self, transport):
        """Expected: no peers on construction."""
        assert transport._peers == {}

    def test_empty_inbox_on_init(self, transport):
        """Expected: inbox queue is empty on construction."""
        assert transport._inbox.empty()


# ---------------------------------------------------------------------------
# configure()
# ---------------------------------------------------------------------------


class TestWebRTCTransportConfigure:
    """Tests for configure()."""

    def test_configure_updates_signaling_url(self, transport):
        """Expected: configure() updates the signaling URL."""
        transport.configure({"signaling_url": "ws://new.broker:1234/ws"})
        assert transport._signaling_url == "ws://new.broker:1234/ws"

    def test_configure_updates_token(self, transport):
        """Expected: configure() updates the CapAuth token."""
        transport.configure({"token": "new-token"})
        assert transport._token == "new-token"

    def test_configure_updates_priority(self, transport):
        """Expected: configure() updates priority."""
        transport.configure({"priority": 5})
        assert transport.priority == 5

    def test_configure_updates_agent_name(self, transport):
        """Expected: configure() updates agent name."""
        transport.configure({"agent_name": "jarvis"})
        assert transport._agent_name == "jarvis"

    def test_configure_updates_turn_secret(self, transport):
        """Expected: configure() updates TURN secret."""
        transport.configure({"turn_secret": "secret-abc"})
        assert transport._turn_secret == "secret-abc"


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestWebRTCTransportAvailability:
    """Tests for is_available()."""

    def test_not_available_when_not_running(self, transport):
        """Expected: False when transport not started."""
        assert transport.is_available() is False

    def test_not_available_when_running_but_no_signaling(self, running_transport):
        """Expected: False when running but signaling not connected."""
        assert running_transport.is_available() is False

    def test_available_when_running_and_signaling_connected(self, connected_transport):
        """Expected: True when running and signaling is connected."""
        assert connected_transport.is_available() is True


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


class TestWebRTCTransportSend:
    """Tests for send()."""

    def test_send_fails_when_not_started(self, transport):
        """Expected: send returns failure when transport not started."""
        result = transport.send(make_envelope(), PEER_FP)
        assert result.success is False
        assert "not started" in result.error.lower()

    def test_send_triggers_ice_when_no_peer(self, running_transport):
        """Expected: send returns failure and schedules ICE negotiation on first send."""
        t = running_transport
        with patch.object(t, "_schedule_offer") as mock_offer:
            result = t.send(make_envelope(), PEER_FP)
        assert result.success is False
        assert "ICE negotiation started" in result.error
        mock_offer.assert_called_once_with(PEER_FP)

    def test_send_does_not_reschedule_when_negotiating(self, running_transport):
        """Expected: _schedule_offer not called again when peer is already negotiating."""
        t = running_transport
        stub = PeerConnection(peer_fingerprint=PEER_FP, pc=None, negotiating=True)
        with t._peers_lock:
            t._peers[PEER_FP] = stub

        with patch.object(t, "_schedule_offer") as mock_offer:
            result = t.send(make_envelope(), PEER_FP)

        assert result.success is False
        mock_offer.assert_not_called()

    def test_send_succeeds_with_connected_channel(self, running_transport):
        """Expected: send returns success when peer has an open data channel."""
        t = running_transport
        mock_channel = MagicMock()

        peer = PeerConnection(
            peer_fingerprint=PEER_FP,
            pc=MagicMock(),
            channel=mock_channel,
            connected=True,
        )
        with t._peers_lock:
            t._peers[PEER_FP] = peer

        # Mock asyncio.run_coroutine_threadsafe to return a completed future
        mock_future = MagicMock()
        mock_future.result.return_value = None

        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
            result = t.send(make_envelope(), PEER_FP)

        assert result.success is True
        assert result.transport_name == "webrtc"

    def test_send_marks_disconnected_on_channel_error(self, running_transport):
        """Expected: peer marked disconnected when channel.send raises."""
        t = running_transport
        mock_channel = MagicMock()
        peer = PeerConnection(
            peer_fingerprint=PEER_FP,
            pc=MagicMock(),
            channel=mock_channel,
            connected=True,
        )
        with t._peers_lock:
            t._peers[PEER_FP] = peer

        mock_future = MagicMock()
        mock_future.result.side_effect = Exception("data channel error")

        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
            result = t.send(make_envelope(), PEER_FP)

        assert result.success is False
        assert t._peers[PEER_FP].connected is False

    def test_send_includes_envelope_id(self, running_transport):
        """Expected: send result includes the envelope ID."""
        t = running_transport
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="id test"),
        )
        with patch.object(t, "_schedule_offer"):
            result = t.send(env.to_bytes(), PEER_FP)
        assert result.envelope_id == env.envelope_id

    def test_send_records_latency(self, running_transport):
        """Expected: send result includes non-negative latency."""
        t = running_transport
        with patch.object(t, "_schedule_offer"):
            result = t.send(make_envelope(), PEER_FP)
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# receive()
# ---------------------------------------------------------------------------


class TestWebRTCTransportReceive:
    """Tests for receive()."""

    def test_receive_empty_inbox(self, transport):
        """Expected: empty list when no messages queued."""
        assert transport.receive() == []

    def test_receive_drains_queue(self, transport):
        """Expected: all queued messages returned."""
        env1, env2 = make_envelope(content="a"), make_envelope(content="b")
        transport._inbox.put(env1)
        transport._inbox.put(env2)
        received = transport.receive()
        assert set(received) == {env1, env2}

    def test_receive_clears_queue(self, transport):
        """Expected: second receive returns empty after first drains."""
        transport._inbox.put(make_envelope())
        transport.receive()
        assert transport.receive() == []


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


class TestWebRTCTransportHealthCheck:
    """Tests for health_check()."""

    def test_unavailable_when_not_started(self, transport):
        """Expected: UNAVAILABLE when transport not started."""
        health = transport.health_check()
        assert health.status == TransportStatus.UNAVAILABLE
        assert health.transport_name == "webrtc"

    def test_degraded_when_running_but_no_signaling(self, running_transport):
        """Expected: DEGRADED when running but signaling disconnected."""
        health = running_transport.health_check()
        assert health.status == TransportStatus.DEGRADED

    def test_available_when_signaling_connected(self, connected_transport):
        """Expected: AVAILABLE when running and signaling connected."""
        health = connected_transport.health_check()
        assert health.status == TransportStatus.AVAILABLE

    def test_health_includes_peer_info(self, connected_transport):
        """Expected: health details include active peer count."""
        t = connected_transport
        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=None, connected=True)
        with t._peers_lock:
            t._peers[PEER_FP] = peer

        health = t.health_check()
        assert health.details["active_peers"] == 1

    def test_health_includes_inbox_pending(self, connected_transport):
        """Expected: health details include pending inbox count."""
        t = connected_transport
        t._inbox.put(make_envelope())
        health = t.health_check()
        assert health.details["inbox_pending"] == 1

    def test_health_degraded_reports_error_message(self, running_transport):
        """Expected: DEGRADED health includes signaling error if set."""
        t = running_transport
        t._signaling_error = "connection refused"
        health = t.health_check()
        assert "connection refused" in (health.error or "")

    def test_health_includes_negotiating_peers(self, connected_transport):
        """Expected: health details report negotiating peers separately."""
        t = connected_transport
        p1 = PeerConnection(peer_fingerprint=PEER_FP, pc=None, connected=True)
        p2_fp = "BBBB9306410CF8CD5E393D6DEC31663B95230684"
        p2 = PeerConnection(peer_fingerprint=p2_fp, pc=None, negotiating=True)
        with t._peers_lock:
            t._peers[PEER_FP] = p1
            t._peers[p2_fp] = p2

        health = t.health_check()
        assert health.details["active_peers"] == 1
        assert health.details["negotiating_peers"] == 1


# ---------------------------------------------------------------------------
# _room_id()
# ---------------------------------------------------------------------------


class TestRoomId:
    """Tests for the signaling room ID generator."""

    def test_room_id_with_fingerprint(self, transport):
        """Expected: room ID uses first 16 chars of fingerprint."""
        room = transport._room_id()
        assert room == f"skcomm-{LOCAL_FP[:16]}"

    def test_room_id_fallback_to_agent_name(self):
        """Expected: room ID uses agent name when no fingerprint."""
        t = WebRTCTransport(agent_name="jarvis")
        assert t._room_id() == "skcomm-jarvis"


# ---------------------------------------------------------------------------
# _extract_id() helper
# ---------------------------------------------------------------------------


class TestExtractId:
    """Tests for the envelope ID extractor."""

    def test_extracts_valid_envelope_id(self):
        """Expected: extracts envelope_id from valid JSON envelope."""
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="test"),
        )
        eid = WebRTCTransport._extract_id(env.to_bytes())
        assert eid == env.envelope_id

    def test_fallback_on_garbage(self):
        """Expected: returns unknown-<ts> for non-JSON bytes."""
        eid = WebRTCTransport._extract_id(b"\x00 not json")
        assert eid.startswith("unknown-")

    def test_fallback_when_key_absent(self):
        """Expected: returns unknown-<ts> when envelope_id key missing."""
        data = json.dumps({"other": "field"}).encode()
        eid = WebRTCTransport._extract_id(data)
        assert eid.startswith("unknown-")


# ---------------------------------------------------------------------------
# _derive_turn_credentials()
# ---------------------------------------------------------------------------


class TestDeriveTurnCredentials:
    """Tests for HMAC-SHA1 TURN credential derivation."""

    def test_credentials_structure(self):
        """Expected: returns (username, credential) with correct format."""
        t = WebRTCTransport(turn_secret="my-hmac-secret", agent_name="opus")
        username, credential = t._derive_turn_credentials()

        assert ":" in username  # "<timestamp>:<agent_name>"
        ts_part, name_part = username.split(":", 1)
        assert name_part == "opus"
        assert int(ts_part) > int(time.time())  # timestamp in the future (+ttl)
        assert len(credential) > 0  # Base64 HMAC

    def test_different_secrets_give_different_credentials(self):
        """Expected: different TURN secrets produce different credentials."""
        t1 = WebRTCTransport(turn_secret="secret1", agent_name="opus")
        t2 = WebRTCTransport(turn_secret="secret2", agent_name="opus")
        _, cred1 = t1._derive_turn_credentials()
        _, cred2 = t2._derive_turn_credentials()
        assert cred1 != cred2


# ---------------------------------------------------------------------------
# _build_ice_servers()
# ---------------------------------------------------------------------------


class TestBuildIceServers:
    """Tests for ICE server configuration builder."""

    def test_stun_only(self):
        """Expected: returns one STUN server when no TURN configured."""
        MockRTCIceServer = MagicMock(side_effect=lambda **kw: kw)
        t = WebRTCTransport(stun_servers=["stun:stun.l.google.com:19302"])
        servers = t._build_ice_servers(MockRTCIceServer)
        assert len(servers) == 1
        assert servers[0]["urls"] == "stun:stun.l.google.com:19302"

    def test_stun_and_turn_with_static_creds(self):
        """Expected: TURN server added with static username/credential."""
        MockRTCIceServer = MagicMock(side_effect=lambda **kw: kw)
        t = WebRTCTransport(
            turn_server="turn:turn.example.com:3478",
            turn_username="user",
            turn_credential="pass",
        )
        servers = t._build_ice_servers(MockRTCIceServer)
        assert len(servers) == 2
        turn = servers[1]
        assert turn["urls"] == "turn:turn.example.com:3478"
        assert turn["username"] == "user"
        assert turn["credential"] == "pass"

    def test_stun_and_turn_with_hmac_secret(self):
        """Expected: TURN server added with HMAC-derived credentials."""
        MockRTCIceServer = MagicMock(side_effect=lambda **kw: kw)
        t = WebRTCTransport(
            turn_server="turn:turn.example.com:3478",
            turn_secret="hmac-secret",
            agent_name="opus",
        )
        servers = t._build_ice_servers(MockRTCIceServer)
        assert len(servers) == 2
        turn = servers[1]
        assert turn["urls"] == "turn:turn.example.com:3478"
        assert "username" in turn
        assert "credential" in turn

    def test_turn_url_only_when_no_creds(self):
        """Expected: TURN server added without creds when neither secret nor static creds set."""
        MockRTCIceServer = MagicMock(side_effect=lambda **kw: kw)
        t = WebRTCTransport(turn_server="turn:bare.example.com:3478")
        servers = t._build_ice_servers(MockRTCIceServer)
        assert len(servers) == 2
        turn = servers[1]
        assert turn["urls"] == "turn:bare.example.com:3478"
        assert "username" not in turn


# ---------------------------------------------------------------------------
# _schedule_offer()
# ---------------------------------------------------------------------------


class TestScheduleOffer:
    """Tests for the synchronous offer scheduling bridge."""

    def test_schedule_offer_installs_stub_peer(self, running_transport):
        """Expected: _schedule_offer creates a negotiating stub peer before dispatching."""
        t = running_transport
        with patch("asyncio.run_coroutine_threadsafe") as mock_dispatch:
            t._schedule_offer(PEER_FP)

        with t._peers_lock:
            stub = t._peers.get(PEER_FP)
        assert stub is not None
        assert stub.negotiating is True

    def test_schedule_offer_dispatches_to_loop(self, running_transport):
        """Expected: _schedule_offer calls run_coroutine_threadsafe with the event loop."""
        t = running_transport
        with patch("asyncio.run_coroutine_threadsafe") as mock_dispatch:
            t._schedule_offer(PEER_FP)
        mock_dispatch.assert_called_once()
        # Second arg is the loop
        assert mock_dispatch.call_args[0][1] is t._loop

    def test_schedule_offer_skips_if_not_running(self, transport):
        """Expected: _schedule_offer does nothing when transport is not running."""
        with patch("asyncio.run_coroutine_threadsafe") as mock_dispatch:
            transport._schedule_offer(PEER_FP)
        mock_dispatch.assert_not_called()

    def test_schedule_offer_does_not_overwrite_existing_peer(self, running_transport):
        """Expected: existing peer not replaced when _schedule_offer called again."""
        t = running_transport
        existing = PeerConnection(peer_fingerprint=PEER_FP, pc=MagicMock(), negotiating=True)
        with t._peers_lock:
            t._peers[PEER_FP] = existing

        with patch("asyncio.run_coroutine_threadsafe"):
            t._schedule_offer(PEER_FP)

        with t._peers_lock:
            assert t._peers[PEER_FP] is existing


# ---------------------------------------------------------------------------
# start() / stop() lifecycle
# ---------------------------------------------------------------------------


class TestWebRTCTransportLifecycle:
    """Tests for start/stop lifecycle."""

    def test_start_sets_running(self):
        """Expected: start() sets _running=True."""
        t = _make_transport()
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            with patch("time.sleep"):  # skip settle delay
                t.start()
        assert t._running is True

    def test_start_is_idempotent(self):
        """Expected: calling start() twice does not start a second thread."""
        t = _make_transport()
        t._running = True
        with patch("threading.Thread") as mock_thread_cls:
            t.start()
        mock_thread_cls.assert_not_called()

    def test_stop_clears_running(self, running_transport):
        """Expected: stop() sets _running=False."""
        t = running_transport
        with patch.object(t, "_async_stop", new_callable=AsyncMock):
            with patch("asyncio.run_coroutine_threadsafe") as mock_rct:
                fut = MagicMock()
                fut.result.return_value = None
                mock_rct.return_value = fut
                t.stop()
        assert t._running is False

    def test_stop_clears_signaling_connected(self, connected_transport):
        """Expected: stop() marks signaling as disconnected."""
        t = connected_transport
        with patch("asyncio.run_coroutine_threadsafe") as mock_rct:
            fut = MagicMock()
            fut.result.return_value = None
            mock_rct.return_value = fut
            t.stop()
        assert t._signaling_connected is False


# ---------------------------------------------------------------------------
# Async: _handle_signal()
# ---------------------------------------------------------------------------


class TestHandleSignal:
    """Tests for the signaling message dispatcher."""

    @pytest.mark.asyncio
    async def test_welcome_initiates_offer_for_peers(self, connected_transport):
        """Expected: 'welcome' message triggers _initiate_offer for each listed peer."""
        t = connected_transport
        with patch.object(t, "_initiate_offer", new_callable=AsyncMock) as mock_offer:
            await t._handle_signal({"type": "welcome", "peers": [PEER_FP]})
        mock_offer.assert_called_once_with(PEER_FP)

    @pytest.mark.asyncio
    async def test_welcome_skips_already_known_peers(self, connected_transport):
        """Expected: 'welcome' does not re-offer to peers already connected."""
        t = connected_transport
        existing = PeerConnection(peer_fingerprint=PEER_FP, pc=MagicMock(), connected=True)
        with t._peers_lock:
            t._peers[PEER_FP] = existing

        with patch.object(t, "_initiate_offer", new_callable=AsyncMock) as mock_offer:
            await t._handle_signal({"type": "welcome", "peers": [PEER_FP]})
        mock_offer.assert_not_called()

    @pytest.mark.asyncio
    async def test_peer_left_closes_connection(self, connected_transport):
        """Expected: 'peer_left' removes peer and closes its PC."""
        t = connected_transport
        mock_pc = MagicMock()
        mock_pc.close = AsyncMock()
        existing = PeerConnection(peer_fingerprint=PEER_FP, pc=mock_pc)
        with t._peers_lock:
            t._peers[PEER_FP] = existing

        await t._handle_signal({"type": "peer_left", "peer": PEER_FP})

        with t._peers_lock:
            assert PEER_FP not in t._peers
        mock_pc.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_signal_dispatches_to_handler(self, connected_transport):
        """Expected: 'signal' message dispatches to _handle_incoming_signal."""
        t = connected_transport
        with patch.object(t, "_handle_incoming_signal", new_callable=AsyncMock) as mock_handler:
            await t._handle_signal({
                "type": "signal",
                "from": PEER_FP,
                "data": {"sdp": {}},
            })
        mock_handler.assert_called_once_with(PEER_FP, {"sdp": {}})

    @pytest.mark.asyncio
    async def test_unknown_signal_type_ignored(self, connected_transport):
        """Expected: unknown signal types are handled without error."""
        t = connected_transport
        # Should not raise
        await t._handle_signal({"type": "unknown_future_type", "peer": PEER_FP})


# ---------------------------------------------------------------------------
# Async: _wait_for_ice_gathering()
# ---------------------------------------------------------------------------


class TestWaitForIceGathering:
    """Tests for the ICE gathering wait helper."""

    @pytest.mark.asyncio
    async def test_returns_immediately_if_complete(self, transport):
        """Expected: returns without waiting if ICE gathering is already complete."""
        mock_pc = MagicMock()
        mock_pc.iceGatheringState = "complete"
        # Should return without waiting or raising
        await transport._wait_for_ice_gathering(mock_pc, timeout=0.1)

    @pytest.mark.asyncio
    async def test_times_out_gracefully(self, transport):
        """Expected: returns after timeout without raising when gathering stalls."""
        mock_pc = MagicMock()
        mock_pc.iceGatheringState = "gathering"
        mock_pc.on = MagicMock()
        # Should log a warning and return (not raise)
        await transport._wait_for_ice_gathering(mock_pc, timeout=0.05)


# ---------------------------------------------------------------------------
# Async: _send_signal()
# ---------------------------------------------------------------------------


class TestSendSignal:
    """Tests for signaling message sending."""

    @pytest.mark.asyncio
    async def test_send_signal_calls_ws_send(self, connected_transport):
        """Expected: _send_signal calls send() on the signaling WebSocket."""
        t = connected_transport
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        t._signaling_ws = mock_ws

        await t._send_signal(to=PEER_FP, data={"sdp": {"type": "offer"}})

        mock_ws.send.assert_called_once()
        sent_text = mock_ws.send.call_args[0][0]
        payload = json.loads(sent_text)
        assert payload["type"] == "signal"
        assert payload["to"] == PEER_FP

    @pytest.mark.asyncio
    async def test_send_signal_skips_when_no_ws(self, transport):
        """Expected: _send_signal is a no-op when not connected."""
        # Should not raise even with no ws
        await transport._send_signal(to=PEER_FP, data={})


# ---------------------------------------------------------------------------
# Async: _async_channel_send()
# ---------------------------------------------------------------------------


class TestAsyncChannelSend:
    """Tests for the data channel send coroutine."""

    @pytest.mark.asyncio
    async def test_calls_channel_send(self):
        """Expected: calls channel.send() with the raw bytes."""
        mock_channel = MagicMock()
        data = b"test payload"
        await WebRTCTransport._async_channel_send(mock_channel, data)
        mock_channel.send.assert_called_once_with(data)


# ---------------------------------------------------------------------------
# _wire_channel() event handlers
# ---------------------------------------------------------------------------


class TestWireChannel:
    """Tests for event handler wiring on RTCDataChannel."""

    def test_wire_channel_sets_peer_channel(self, transport):
        """Expected: _wire_channel assigns the channel to the peer."""
        mock_channel = MagicMock()
        mock_channel.on = MagicMock()
        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=MagicMock())
        transport._wire_channel(peer, mock_channel)
        assert peer.channel is mock_channel

    def test_message_handler_buffers_bytes(self, transport):
        """Expected: message handler puts bytes into inbox."""
        mock_channel = MagicMock()
        registered_handlers = {}

        def on_handler(event):
            def decorator(fn):
                registered_handlers[event] = fn
                return fn
            return decorator

        mock_channel.on = on_handler
        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=MagicMock())
        transport._wire_channel(peer, mock_channel)

        # Trigger the message handler
        registered_handlers["message"](b"hello world")
        assert transport._inbox.get_nowait() == b"hello world"

    def test_message_handler_coerces_str_to_bytes(self, transport):
        """Expected: string messages are converted to bytes."""
        mock_channel = MagicMock()
        registered_handlers = {}

        def on_handler(event):
            def decorator(fn):
                registered_handlers[event] = fn
                return fn
            return decorator

        mock_channel.on = on_handler
        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=MagicMock())
        transport._wire_channel(peer, mock_channel)

        registered_handlers["message"]("string message")
        msg = transport._inbox.get_nowait()
        assert isinstance(msg, bytes)

    def test_close_handler_marks_disconnected(self, transport):
        """Expected: close handler sets peer.connected=False."""
        mock_channel = MagicMock()
        registered_handlers = {}

        def on_handler(event):
            def decorator(fn):
                registered_handlers[event] = fn
                return fn
            return decorator

        mock_channel.on = on_handler
        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=MagicMock(), connected=True)
        transport._wire_channel(peer, mock_channel)

        registered_handlers["close"]()
        assert peer.connected is False


# ---------------------------------------------------------------------------
# create_transport() factory
# ---------------------------------------------------------------------------


class TestCreateTransportFactory:
    """Tests for the create_transport factory function."""

    def test_factory_returns_webrtc_transport(self):
        """Expected: factory returns a WebRTCTransport instance."""
        t = create_transport()
        assert isinstance(t, WebRTCTransport)

    def test_factory_default_priority(self):
        """Expected: factory uses priority=1 by default."""
        t = create_transport()
        assert t.priority == 1

    def test_factory_custom_signaling_url(self):
        """Expected: factory passes signaling URL through."""
        t = create_transport(signaling_url="ws://custom:9999/ws")
        assert t._signaling_url == "ws://custom:9999/ws"

    def test_factory_custom_token(self):
        """Expected: factory passes CapAuth token through."""
        t = create_transport(token="token-xyz")
        assert t._token == "token-xyz"

    def test_factory_custom_fingerprint(self):
        """Expected: factory passes agent fingerprint through."""
        t = create_transport(agent_fingerprint=LOCAL_FP)
        assert t._agent_fingerprint == LOCAL_FP

    def test_factory_not_running(self):
        """Expected: factory does not start the transport by default."""
        t = create_transport()
        assert t._running is False
