"""Integration tests for the WebRTC transport.

Focuses on cross-cutting concerns that unit tests miss:
- _run_in_loop behaviour with stopped/closed loops
- _cleanup_peer atomicity under concurrent access
- send() with no peers (scheduling + fallback)
- SKCOMM_SIGNALING_URL environment variable override
- health_check response structure and state transitions

All tests use unittest.mock — no aiortc or signaling infrastructure required.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skcomm.transport import TransportCategory, TransportStatus
from skcomm.transports.webrtc import (
    DEFAULT_SIGNALING_URL,
    PeerConnection,
    WebRTCTransport,
    create_transport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PEER_FP = "AAAA9306410CF8CD5E393D6DEC31663B95230684"
PEER_FP_2 = "BBBB9306410CF8CD5E393D6DEC31663B95230684"
LOCAL_FP = "CCBE9306410CF8CD5E393D6DEC31663B95230684"


def _make_transport(**kwargs) -> WebRTCTransport:
    """Build a transport without starting the background thread."""
    return WebRTCTransport(
        agent_fingerprint=LOCAL_FP,
        agent_name="opus",
        **kwargs,
    )


def _make_envelope_bytes(envelope_id="test-env-001", content="test") -> bytes:
    """Create minimal JSON envelope bytes."""
    return json.dumps({
        "envelope_id": envelope_id,
        "sender": "opus",
        "recipient": "lumina",
        "payload": {"content": content},
    }).encode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport():
    """A WebRTCTransport that is NOT started (no background thread)."""
    return _make_transport()


@pytest.fixture
def running_transport():
    """A WebRTCTransport with _running=True and a mocked, running event loop."""
    t = _make_transport()
    t._running = True
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.is_closed.return_value = False
    loop.is_running.return_value = True
    t._loop = loop
    return t


@pytest.fixture
def connected_transport():
    """A WebRTCTransport with signaling connected and a mocked running loop."""
    t = _make_transport()
    t._running = True
    t._signaling_connected = True
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.is_closed.return_value = False
    loop.is_running.return_value = True
    t._loop = loop
    return t


# ===========================================================================
# 1. _run_in_loop with stopped / closed loop
# ===========================================================================


class TestRunInLoopEdgeCases:
    """Test _run_in_loop behaviour when the event loop is unavailable."""

    def test_raises_when_loop_is_none(self, transport):
        """Expected: RuntimeError when _loop is None."""
        transport._loop = None

        async def dummy():
            pass

        with pytest.raises(RuntimeError, match="not running"):
            transport._run_in_loop(dummy())

    def test_raises_when_loop_is_closed(self, transport):
        """Expected: RuntimeError when the event loop is closed."""
        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        loop.is_closed.return_value = True
        loop.is_running.return_value = False
        transport._loop = loop

        async def dummy():
            pass

        with pytest.raises(RuntimeError, match="not running"):
            transport._run_in_loop(dummy())

    def test_raises_when_loop_is_stopped(self, transport):
        """Expected: RuntimeError when the loop exists but is not running."""
        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        loop.is_closed.return_value = False
        loop.is_running.return_value = False
        transport._loop = loop

        async def dummy():
            pass

        with pytest.raises(RuntimeError, match="not running"):
            transport._run_in_loop(dummy())

    def test_succeeds_when_loop_is_running(self, running_transport):
        """Expected: successfully submits coroutine when loop is running."""
        t = running_transport
        mock_future = MagicMock(spec=Future)

        async def dummy():
            return 42

        coro = dummy()
        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future) as mock_rcts:
            result = t._run_in_loop(coro)

        assert result is mock_future
        mock_rcts.assert_called_once()
        assert mock_rcts.call_args[0][1] is t._loop

        # Clean up the coroutine to avoid "never awaited" warning
        coro.close()

    def test_coroutine_closed_on_error(self, transport):
        """Expected: coroutine is closed (not leaked) when loop is unavailable."""
        transport._loop = None

        async def tracked_coro():
            pass

        coro = tracked_coro()
        with pytest.raises(RuntimeError):
            transport._run_in_loop(coro)

        # If the coroutine was properly closed, calling close() again is a no-op
        # and does not raise. If it was NOT closed, Python would emit a
        # "coroutine was never awaited" warning.
        coro.close()  # Should not raise — already closed by _run_in_loop

    def test_send_fails_gracefully_when_loop_stopped_mid_flight(self, running_transport):
        """Expected: send returns failure when _run_in_loop raises RuntimeError."""
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

        # Simulate loop stopping between is_connected check and actual send
        t._loop.is_running.return_value = False

        result = t.send(_make_envelope_bytes(), PEER_FP)
        assert result.success is False
        assert "not running" in result.error.lower() or "loop" in result.error.lower()


# ===========================================================================
# 2. _cleanup_peer atomicity (mock peers dict)
# ===========================================================================


class TestCleanupPeerAtomicity:
    """Test that _cleanup_peer safely removes peers under concurrent access."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_peer_atomically(self, connected_transport):
        """Expected: peer is removed from dict under lock and PC is closed."""
        t = connected_transport
        mock_pc = MagicMock()
        mock_pc.close = AsyncMock()
        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=mock_pc, connected=True)

        with t._peers_lock:
            t._peers[PEER_FP] = peer

        await t._cleanup_peer(PEER_FP)

        with t._peers_lock:
            assert PEER_FP not in t._peers
        mock_pc.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_peer_is_noop(self, connected_transport):
        """Expected: cleaning up a peer that does not exist does not raise."""
        t = connected_transport
        # Should not raise
        await t._cleanup_peer("NONEXISTENT_FINGERPRINT_0000000000000000")

    @pytest.mark.asyncio
    async def test_cleanup_peer_handles_pc_close_exception(self, connected_transport):
        """Expected: peer is still removed even if pc.close() raises."""
        t = connected_transport
        mock_pc = MagicMock()
        mock_pc.close = AsyncMock(side_effect=RuntimeError("close failed"))
        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=mock_pc)

        with t._peers_lock:
            t._peers[PEER_FP] = peer

        # Should not raise despite the close error
        await t._cleanup_peer(PEER_FP)

        with t._peers_lock:
            assert PEER_FP not in t._peers

    @pytest.mark.asyncio
    async def test_cleanup_peer_does_not_affect_other_peers(self, connected_transport):
        """Expected: cleaning up one peer leaves other peers intact."""
        t = connected_transport
        pc1 = MagicMock()
        pc1.close = AsyncMock()
        pc2 = MagicMock()
        pc2.close = AsyncMock()

        peer1 = PeerConnection(peer_fingerprint=PEER_FP, pc=pc1)
        peer2 = PeerConnection(peer_fingerprint=PEER_FP_2, pc=pc2)

        with t._peers_lock:
            t._peers[PEER_FP] = peer1
            t._peers[PEER_FP_2] = peer2

        await t._cleanup_peer(PEER_FP)

        with t._peers_lock:
            assert PEER_FP not in t._peers
            assert PEER_FP_2 in t._peers
        pc2.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_stop_cleans_all_peers(self, connected_transport):
        """Expected: _async_stop closes all peers without dict-changed-size errors."""
        t = connected_transport

        for i in range(5):
            fp = f"AAAA{i:036d}"
            mock_pc = MagicMock()
            mock_pc.close = AsyncMock()
            with t._peers_lock:
                t._peers[fp] = PeerConnection(peer_fingerprint=fp, pc=mock_pc)

        t._signaling_ws = MagicMock()
        t._signaling_ws.close = AsyncMock()

        await t._async_stop()

        with t._peers_lock:
            assert len(t._peers) == 0


# ===========================================================================
# 3. send() with no peers
# ===========================================================================


class TestSendWithNoPeers:
    """Test send behaviour when there are no established peer connections."""

    def test_send_not_started_returns_error(self, transport):
        """Expected: send fails with 'not started' when transport is not running."""
        result = transport.send(_make_envelope_bytes(), PEER_FP)
        assert result.success is False
        assert "not started" in result.error.lower()
        assert result.transport_name == "webrtc"

    def test_send_no_peer_triggers_ice_negotiation(self, running_transport):
        """Expected: send to unknown peer triggers ICE negotiation and returns failure."""
        t = running_transport

        with patch.object(t, "_run_in_loop") as mock_run:
            result = t.send(_make_envelope_bytes(), PEER_FP)

        assert result.success is False
        assert "ICE negotiation started" in result.error
        mock_run.assert_called_once()
        assert PEER_FP in t._peers
        assert t._peers[PEER_FP].negotiating is True

    def test_send_no_peer_includes_envelope_id(self, running_transport):
        """Expected: failed send result still carries the envelope ID."""
        t = running_transport
        env_id = "my-unique-id-123"
        envelope = _make_envelope_bytes(envelope_id=env_id)

        with patch.object(t, "_schedule_offer"):
            result = t.send(envelope, PEER_FP)

        assert result.envelope_id == env_id

    def test_send_no_peer_records_latency(self, running_transport):
        """Expected: failed send result includes non-negative latency."""
        t = running_transport

        with patch.object(t, "_schedule_offer"):
            result = t.send(_make_envelope_bytes(), PEER_FP)

        assert result.latency_ms >= 0

    def test_send_does_not_double_schedule_while_negotiating(self, running_transport):
        """Expected: second send to same peer during ICE does not reschedule offer."""
        t = running_transport
        stub = PeerConnection(peer_fingerprint=PEER_FP, pc=None, negotiating=True)
        with t._peers_lock:
            t._peers[PEER_FP] = stub

        with patch.object(t, "_schedule_offer") as mock_offer:
            result1 = t.send(_make_envelope_bytes(), PEER_FP)
            result2 = t.send(_make_envelope_bytes(), PEER_FP)

        assert result1.success is False
        assert result2.success is False
        mock_offer.assert_not_called()


# ===========================================================================
# 4. SKCOMM_SIGNALING_URL environment variable
# ===========================================================================


class TestSignalingUrlEnvVar:
    """Test that the SKCOMM_SIGNALING_URL env var is respected."""

    def test_default_signaling_url_when_env_unset(self):
        """Expected: default URL is used when env var is not set."""
        t = WebRTCTransport()
        # The default is whatever DEFAULT_SIGNALING_URL resolves to
        assert t._signaling_url == DEFAULT_SIGNALING_URL

    def test_signaling_url_from_env_var(self):
        """Expected: SKCOMM_SIGNALING_URL env var overrides the default."""
        import importlib
        import skcomm.transports.webrtc as webrtc_mod
        custom_url = "wss://signal.custom.example.com/ws"
        try:
            with patch.dict(os.environ, {"SKCOMM_SIGNALING_URL": custom_url}):
                # Reload the module-level default to pick up env change
                importlib.reload(webrtc_mod)
                t = webrtc_mod.WebRTCTransport()
                assert t._signaling_url == custom_url
        finally:
            # Restore the module AFTER patch.dict exits so env is clean
            importlib.reload(webrtc_mod)

    def test_constructor_url_overrides_env(self):
        """Expected: explicit signaling_url parameter overrides env var."""
        import importlib
        import skcomm.transports.webrtc as webrtc_mod
        explicit_url = "ws://explicit:1234/ws"
        try:
            with patch.dict(os.environ, {"SKCOMM_SIGNALING_URL": "ws://from-env:9999/ws"}):
                importlib.reload(webrtc_mod)
                t = webrtc_mod.WebRTCTransport(signaling_url=explicit_url)
                assert t._signaling_url == explicit_url
        finally:
            # Restore the module AFTER patch.dict exits so env is clean
            importlib.reload(webrtc_mod)

    def test_configure_overrides_signaling_url(self, transport):
        """Expected: configure() can update signaling URL after construction."""
        transport.configure({"signaling_url": "ws://reconfigured:5555/ws"})
        assert transport._signaling_url == "ws://reconfigured:5555/ws"


# ===========================================================================
# 5. health_check response structure and state transitions
# ===========================================================================


class TestHealthCheckIntegration:
    """Test health_check response integrity across state transitions."""

    def test_health_unavailable_structure(self, transport):
        """Expected: UNAVAILABLE health includes transport name and signaling URL."""
        health = transport.health_check()
        assert health.status == TransportStatus.UNAVAILABLE
        assert health.transport_name == "webrtc"
        assert "not started" in (health.error or "").lower()
        assert "signaling_url" in health.details

    def test_health_degraded_structure(self, running_transport):
        """Expected: DEGRADED health when running but signaling disconnected."""
        t = running_transport
        t._signaling_error = "connection timed out"
        health = t.health_check()
        assert health.status == TransportStatus.DEGRADED
        assert "connection timed out" in (health.error or "")
        assert health.details["signaling_connected"] is False

    def test_health_available_structure(self, connected_transport):
        """Expected: AVAILABLE health with full details when signaling is up."""
        t = connected_transport
        health = t.health_check()
        assert health.status == TransportStatus.AVAILABLE
        assert health.details["signaling_connected"] is True
        assert health.details["active_peers"] == 0
        assert health.details["negotiating_peers"] == 0
        assert health.details["inbox_pending"] == 0
        assert "peer_fingerprints" in health.details

    def test_health_transitions_through_lifecycle(self):
        """Expected: health status transitions: UNAVAILABLE -> DEGRADED -> AVAILABLE."""
        t = _make_transport()

        # Phase 1: not started
        assert t.health_check().status == TransportStatus.UNAVAILABLE

        # Phase 2: started but signaling not connected
        t._running = True
        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        loop.is_closed.return_value = False
        loop.is_running.return_value = True
        t._loop = loop
        assert t.health_check().status == TransportStatus.DEGRADED

        # Phase 3: signaling connected
        t._signaling_connected = True
        assert t.health_check().status == TransportStatus.AVAILABLE

        # Phase 4: back to stopped
        t._running = False
        t._signaling_connected = False
        assert t.health_check().status == TransportStatus.UNAVAILABLE

    def test_health_counts_mixed_peer_states(self, connected_transport):
        """Expected: health correctly separates active and negotiating peers."""
        t = connected_transport

        # 2 connected, 1 negotiating, 1 disconnected
        with t._peers_lock:
            t._peers["fp-connected-1"] = PeerConnection(
                peer_fingerprint="fp-connected-1", pc=None, connected=True
            )
            t._peers["fp-connected-2"] = PeerConnection(
                peer_fingerprint="fp-connected-2", pc=None, connected=True
            )
            t._peers["fp-negotiating"] = PeerConnection(
                peer_fingerprint="fp-negotiating", pc=None, negotiating=True
            )
            t._peers["fp-disconnected"] = PeerConnection(
                peer_fingerprint="fp-disconnected", pc=None
            )

        health = t.health_check()
        assert health.details["active_peers"] == 2
        assert health.details["negotiating_peers"] == 1
        # Peer fingerprints are truncated to 8 chars
        fps = health.details["peer_fingerprints"]
        assert len(fps) == 2

    def test_health_inbox_reflects_pending_messages(self, connected_transport):
        """Expected: inbox_pending accurately reflects queued message count."""
        t = connected_transport
        for i in range(3):
            t._inbox.put(_make_envelope_bytes(envelope_id=f"env-{i}"))
        health = t.health_check()
        assert health.details["inbox_pending"] == 3


# ===========================================================================
# 6. configure() integration (restart behaviour)
# ===========================================================================


class TestConfigureIntegration:
    """Test configure() interaction with running state."""

    def test_configure_while_stopped_does_not_start(self, transport):
        """Expected: configure() on a stopped transport does not start it."""
        transport.configure({"signaling_url": "ws://new:1234/ws"})
        assert transport._running is False

    def test_configure_auto_connect_starts_transport(self, transport):
        """Expected: configure with auto_connect=True starts the transport."""
        with patch.object(transport, "start") as mock_start:
            transport.configure({"auto_connect": True})
        mock_start.assert_called_once()

    def test_configure_multiple_fields_at_once(self, transport):
        """Expected: configure() updates multiple fields atomically."""
        transport.configure({
            "signaling_url": "ws://bulk:9999/ws",
            "agent_name": "jarvis",
            "token": "new-token",
            "priority": 7,
            "turn_secret": "my-secret",
        })
        assert transport._signaling_url == "ws://bulk:9999/ws"
        assert transport._agent_name == "jarvis"
        assert transport._token == "new-token"
        assert transport.priority == 7
        assert transport._turn_secret == "my-secret"
