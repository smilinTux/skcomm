"""Tests for the WebRTC signaling broker (WebRTCRoom, SignalingBroker, endpoint)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skcomm.signaling import SignalingBroker, WebRTCRoom, signaling_ws_endpoint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PEER_A = "AAAA9306410CF8CD5E393D6DEC31663B95230684"
PEER_B = "BBBB9306410CF8CD5E393D6DEC31663B95230684"
PEER_C = "CCCC9306410CF8CD5E393D6DEC31663B95230684"
ROOM_ID = "skcomm-test-room"


def _mock_ws():
    """Create a mock WebSocket with async send_text."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.headers = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    return ws


def _sent_messages(ws_mock) -> list[dict]:
    """Extract all JSON messages sent over a mock WebSocket."""
    calls = ws_mock.send_text.call_args_list
    return [json.loads(c[0][0]) for c in calls]


# ---------------------------------------------------------------------------
# WebRTCRoom — basic state
# ---------------------------------------------------------------------------


class TestWebRTCRoomProperties:
    """Tests for room property accessors."""

    def test_empty_room_on_init(self):
        """Expected: room starts with no peers."""
        room = WebRTCRoom(ROOM_ID)
        assert room.peer_ids == []
        assert room.is_empty is True

    def test_room_id_stored(self):
        """Expected: room_id attribute is set correctly."""
        room = WebRTCRoom("my-room")
        assert room.room_id == "my-room"


# ---------------------------------------------------------------------------
# WebRTCRoom.add_peer()
# ---------------------------------------------------------------------------


class TestWebRTCRoomAddPeer:
    """Tests for add_peer() behaviour."""

    @pytest.mark.asyncio
    async def test_add_peer_sends_welcome(self):
        """Expected: new peer receives a welcome message with current peer list."""
        room = WebRTCRoom(ROOM_ID)
        ws_a = _mock_ws()
        await room.add_peer(PEER_A, ws_a)

        msgs = _sent_messages(ws_a)
        welcome_msgs = [m for m in msgs if m.get("type") == "welcome"]
        assert len(welcome_msgs) == 1
        assert welcome_msgs[0]["peers"] == []  # room was empty before A joined

    @pytest.mark.asyncio
    async def test_add_second_peer_welcome_contains_first(self):
        """Expected: second peer's welcome lists the first peer."""
        room = WebRTCRoom(ROOM_ID)
        ws_a = _mock_ws()
        ws_b = _mock_ws()

        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)

        msgs_b = _sent_messages(ws_b)
        welcome = next(m for m in msgs_b if m.get("type") == "welcome")
        assert PEER_A in welcome["peers"]

    @pytest.mark.asyncio
    async def test_add_peer_notifies_existing_peers(self):
        """Expected: existing peer receives peer_joined notification."""
        room = WebRTCRoom(ROOM_ID)
        ws_a = _mock_ws()
        ws_b = _mock_ws()

        await room.add_peer(PEER_A, ws_a)
        ws_a.send_text.reset_mock()  # clear the welcome message

        await room.add_peer(PEER_B, ws_b)

        msgs_a = _sent_messages(ws_a)
        peer_joined = [m for m in msgs_a if m.get("type") == "peer_joined"]
        assert len(peer_joined) == 1
        assert peer_joined[0]["peer"] == PEER_B

    @pytest.mark.asyncio
    async def test_peer_not_in_empty_when_same_fingerprint(self):
        """Expected: adding same fingerprint twice replaces old entry (no duplicate notification)."""
        room = WebRTCRoom(ROOM_ID)
        ws_a1 = _mock_ws()
        ws_a2 = _mock_ws()

        await room.add_peer(PEER_A, ws_a1)
        await room.add_peer(PEER_A, ws_a2)

        assert len(room.peer_ids) == 1

    @pytest.mark.asyncio
    async def test_add_peer_updates_peer_list(self):
        """Expected: peer_ids contains all added fingerprints."""
        room = WebRTCRoom(ROOM_ID)
        ws_a, ws_b = _mock_ws(), _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)
        assert PEER_A in room.peer_ids
        assert PEER_B in room.peer_ids
        assert room.is_empty is False


# ---------------------------------------------------------------------------
# WebRTCRoom.remove_peer()
# ---------------------------------------------------------------------------


class TestWebRTCRoomRemovePeer:
    """Tests for remove_peer() behaviour."""

    @pytest.mark.asyncio
    async def test_remove_peer_notifies_remaining(self):
        """Expected: remaining peer receives peer_left notification."""
        room = WebRTCRoom(ROOM_ID)
        ws_a, ws_b = _mock_ws(), _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)
        ws_a.send_text.reset_mock()

        await room.remove_peer(PEER_B)

        msgs_a = _sent_messages(ws_a)
        peer_left = [m for m in msgs_a if m.get("type") == "peer_left"]
        assert len(peer_left) == 1
        assert peer_left[0]["peer"] == PEER_B

    @pytest.mark.asyncio
    async def test_remove_peer_empties_room(self):
        """Expected: room reports is_empty after last peer leaves."""
        room = WebRTCRoom(ROOM_ID)
        ws_a = _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.remove_peer(PEER_A)
        assert room.is_empty is True

    @pytest.mark.asyncio
    async def test_remove_nonexistent_peer_safe(self):
        """Expected: removing a peer that's not in the room raises no error."""
        room = WebRTCRoom(ROOM_ID)
        await room.remove_peer("nonexistent-fingerprint")  # must not raise


# ---------------------------------------------------------------------------
# WebRTCRoom.relay()
# ---------------------------------------------------------------------------


class TestWebRTCRoomRelay:
    """Tests for the signal relay method."""

    @pytest.mark.asyncio
    async def test_relay_delivers_to_target(self):
        """Expected: relay sends the signal to the target peer's WebSocket."""
        room = WebRTCRoom(ROOM_ID)
        ws_a, ws_b = _mock_ws(), _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)
        ws_b.send_text.reset_mock()

        sdp = {"sdp": {"type": "offer", "sdp": "v=0..."}}
        result = await room.relay(sender=PEER_A, to=PEER_B, data=sdp)

        assert result is True
        msgs_b = _sent_messages(ws_b)
        signal_msgs = [m for m in msgs_b if m.get("type") == "signal"]
        assert len(signal_msgs) == 1
        assert signal_msgs[0]["from"] == PEER_A
        assert signal_msgs[0]["data"] == sdp

    @pytest.mark.asyncio
    async def test_relay_uses_authenticated_sender(self):
        """Expected: relay stamps the authenticated sender in the 'from' field."""
        room = WebRTCRoom(ROOM_ID)
        ws_a, ws_b = _mock_ws(), _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)
        ws_b.send_text.reset_mock()

        await room.relay(sender=PEER_A, to=PEER_B, data={})
        msg = json.loads(ws_b.send_text.call_args[0][0])
        assert msg["from"] == PEER_A

    @pytest.mark.asyncio
    async def test_relay_returns_false_for_absent_target(self):
        """Expected: relay returns False when target peer is not in the room."""
        room = WebRTCRoom(ROOM_ID)
        ws_a = _mock_ws()
        await room.add_peer(PEER_A, ws_a)

        result = await room.relay(sender=PEER_A, to=PEER_C, data={"sdp": {}})
        assert result is False

    @pytest.mark.asyncio
    async def test_relay_returns_false_on_send_error(self):
        """Expected: relay returns False when WebSocket.send_text raises."""
        room = WebRTCRoom(ROOM_ID)
        ws_a, ws_b = _mock_ws(), _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)
        ws_b.send_text.side_effect = Exception("ws closed")

        result = await room.relay(sender=PEER_A, to=PEER_B, data={})
        assert result is False


# ---------------------------------------------------------------------------
# WebRTCRoom._notify_others()
# ---------------------------------------------------------------------------


class TestWebRTCRoomNotifyOthers:
    """Tests for _notify_others broadcast."""

    @pytest.mark.asyncio
    async def test_notify_skips_sender(self):
        """Expected: sender does not receive their own broadcast."""
        room = WebRTCRoom(ROOM_ID)
        ws_a, ws_b = _mock_ws(), _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)
        ws_a.send_text.reset_mock()
        ws_b.send_text.reset_mock()

        await room._notify_others(sender=PEER_A, message={"type": "custom"})

        # PEER_B should receive it
        assert ws_b.send_text.call_count == 1
        # PEER_A should NOT receive it
        assert ws_a.send_text.call_count == 0

    @pytest.mark.asyncio
    async def test_notify_tolerates_send_failure(self):
        """Expected: _notify_others does not raise when a peer WS fails."""
        room = WebRTCRoom(ROOM_ID)
        ws_a, ws_b, ws_c = _mock_ws(), _mock_ws(), _mock_ws()
        await room.add_peer(PEER_A, ws_a)
        await room.add_peer(PEER_B, ws_b)
        await room.add_peer(PEER_C, ws_c)
        ws_b.send_text.side_effect = Exception("gone")

        # Should not raise even though PEER_B send fails
        await room._notify_others(sender=PEER_A, message={"type": "test"})
        assert ws_c.send_text.call_count >= 1


# ---------------------------------------------------------------------------
# SignalingBroker — room management
# ---------------------------------------------------------------------------


class TestSignalingBrokerRooms:
    """Tests for room lifecycle management in SignalingBroker."""

    def test_get_or_create_room_creates_new(self):
        """Expected: get_or_create_room creates a new room when absent."""
        broker = SignalingBroker(require_auth=False)
        room = broker.get_or_create_room("room-1")
        assert isinstance(room, WebRTCRoom)
        assert room.room_id == "room-1"

    def test_get_or_create_room_returns_existing(self):
        """Expected: same room object returned for the same room_id."""
        broker = SignalingBroker(require_auth=False)
        room1 = broker.get_or_create_room("room-1")
        room2 = broker.get_or_create_room("room-1")
        assert room1 is room2

    def test_cleanup_removes_empty_room(self):
        """Expected: cleanup_room deletes the room when it has no peers."""
        broker = SignalingBroker(require_auth=False)
        broker.get_or_create_room("empty-room")
        broker.cleanup_room("empty-room")
        assert "empty-room" not in broker._rooms

    @pytest.mark.asyncio
    async def test_cleanup_preserves_non_empty_room(self):
        """Expected: cleanup_room does not delete a room that still has peers."""
        broker = SignalingBroker(require_auth=False)
        room = broker.get_or_create_room("busy-room")
        ws_a = _mock_ws()
        await room.add_peer(PEER_A, ws_a)

        broker.cleanup_room("busy-room")
        assert "busy-room" in broker._rooms

    def test_cleanup_nonexistent_room_safe(self):
        """Expected: cleanup_room with unknown room_id does not raise."""
        broker = SignalingBroker(require_auth=False)
        broker.cleanup_room("never-existed")  # no error

    @pytest.mark.asyncio
    async def test_active_rooms_snapshot(self):
        """Expected: active_rooms returns mapping of room_id → peer list."""
        broker = SignalingBroker(require_auth=False)
        room = broker.get_or_create_room("r1")
        ws_a = _mock_ws()
        await room.add_peer(PEER_A, ws_a)

        snapshot = broker.active_rooms()
        assert "r1" in snapshot
        assert PEER_A in snapshot["r1"]


# ---------------------------------------------------------------------------
# SignalingBroker.authenticate()
# ---------------------------------------------------------------------------


class TestSignalingBrokerAuthenticate:
    """Tests for the authenticate() header parser."""

    def test_valid_bearer_token_passes_to_validator(self):
        """Expected: authenticate() strips 'Bearer ' and calls validator."""
        mock_validator = MagicMock()
        mock_validator.validate.return_value = PEER_A
        broker = SignalingBroker(validator=mock_validator)

        result = broker.authenticate(f"Bearer {PEER_A}")
        mock_validator.validate.assert_called_once_with(PEER_A)
        assert result == PEER_A

    def test_missing_header_passes_none_to_validator(self):
        """Expected: authenticate(None) calls validator with None."""
        mock_validator = MagicMock()
        mock_validator.validate.return_value = None
        broker = SignalingBroker(validator=mock_validator)

        result = broker.authenticate(None)
        mock_validator.validate.assert_called_once_with(None)
        assert result is None

    def test_non_bearer_header_passes_none(self):
        """Expected: authenticate() with non-Bearer header passes None."""
        mock_validator = MagicMock()
        mock_validator.validate.return_value = None
        broker = SignalingBroker(validator=mock_validator)

        broker.authenticate("Basic dXNlcjpwYXNz")
        mock_validator.validate.assert_called_once_with(None)

    def test_bearer_case_insensitive(self):
        """Expected: 'bearer' (lowercase) is recognised."""
        mock_validator = MagicMock()
        mock_validator.validate.return_value = PEER_A
        broker = SignalingBroker(validator=mock_validator)

        result = broker.authenticate(f"bearer {PEER_A}")
        mock_validator.validate.assert_called_once_with(PEER_A)
        assert result == PEER_A


# ---------------------------------------------------------------------------
# SignalingBroker.handle_connection()
# ---------------------------------------------------------------------------


class TestSignalingBrokerHandleConnection:
    """Tests for the full WebSocket session lifecycle."""

    @pytest.mark.asyncio
    async def test_handle_connection_joins_room(self):
        """Expected: peer is added to room on connection."""
        broker = SignalingBroker(require_auth=False)
        ws = _mock_ws()
        ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))

        await broker.handle_connection(ws=ws, room_id=ROOM_ID, peer_id=PEER_A)

        # Room should have been created (and then cleaned up since peer left)
        # The test just ensures no exception is raised and welcome was sent
        welcome_calls = [
            call for call in ws.send_text.call_args_list
            if '"welcome"' in call[0][0]
        ]
        assert len(welcome_calls) == 1

    @pytest.mark.asyncio
    async def test_handle_connection_relays_signal(self):
        """Expected: signal messages are relayed to the target peer."""
        broker = SignalingBroker(require_auth=False)
        room = broker.get_or_create_room(ROOM_ID)

        ws_b = _mock_ws()
        await room.add_peer(PEER_B, ws_b)
        ws_b.send_text.reset_mock()

        # Simulate PEER_A connecting and sending a signal to PEER_B
        signal_msg = json.dumps({"type": "signal", "to": PEER_B, "data": {"sdp": {}}})
        ws_a = _mock_ws()
        ws_a.receive_text = AsyncMock(side_effect=[signal_msg, Exception("disconnect")])

        await broker.handle_connection(ws=ws_a, room_id=ROOM_ID, peer_id=PEER_A)

        # PEER_B should have received a signal from PEER_A
        relayed = [
            json.loads(c[0][0]) for c in ws_b.send_text.call_args_list
            if '"signal"' in c[0][0]
        ]
        assert len(relayed) == 1
        assert relayed[0]["from"] == PEER_A

    @pytest.mark.asyncio
    async def test_handle_connection_cleans_up_on_disconnect(self):
        """Expected: peer is removed and empty room is cleaned up on disconnect."""
        broker = SignalingBroker(require_auth=False)
        ws = _mock_ws()
        ws.receive_text = AsyncMock(side_effect=Exception("ws disconnected"))

        await broker.handle_connection(ws=ws, room_id="cleanup-room", peer_id=PEER_A)

        # Room should be cleaned up since it became empty
        assert "cleanup-room" not in broker._rooms


# ---------------------------------------------------------------------------
# signaling_ws_endpoint()
# ---------------------------------------------------------------------------


class TestSignalingWsEndpoint:
    """Tests for the FastAPI WebSocket endpoint handler."""

    @pytest.mark.asyncio
    async def test_unauthenticated_connection_closed_4401(self):
        """Expected: endpoint closes with 4401 when authentication fails."""
        mock_validator = MagicMock()
        mock_validator.validate.return_value = None
        broker = SignalingBroker(validator=mock_validator)

        ws = _mock_ws()
        ws.headers = MagicMock()
        ws.headers.get = MagicMock(return_value=None)

        await signaling_ws_endpoint(ws=ws, room=ROOM_ID, peer=PEER_A, broker=broker)

        ws.accept.assert_called_once()
        ws.close.assert_called_once_with(code=4401, reason=pytest.approx("Unauthorized: invalid or missing CapAuth token", rel=1e-3))

    @pytest.mark.asyncio
    async def test_authenticated_connection_uses_auth_fp(self):
        """Expected: endpoint uses the authenticated fingerprint, not the peer param."""
        mock_validator = MagicMock()
        mock_validator.validate.return_value = PEER_A  # authenticated as PEER_A
        broker = SignalingBroker(validator=mock_validator)

        # The broker's handle_connection is async; we patch it to capture peer_id
        captured = {}

        async def fake_handle(ws, room_id, peer_id):
            captured["peer_id"] = peer_id
            # Normal return — no raise needed

        broker.handle_connection = fake_handle

        ws = _mock_ws()
        ws.headers = MagicMock()
        ws.headers.get = MagicMock(return_value=f"Bearer {PEER_A}")

        await signaling_ws_endpoint(ws=ws, room=ROOM_ID, peer="spoofed-peer", broker=broker)

        # Authenticated fingerprint PEER_A is used, NOT "spoofed-peer"
        assert captured.get("peer_id") == PEER_A

    @pytest.mark.asyncio
    async def test_anonymous_connection_uses_peer_param(self):
        """Expected: 'anonymous' auth uses the peer query param as peer_id."""
        mock_validator = MagicMock()
        mock_validator.validate.return_value = "anonymous"
        broker = SignalingBroker(validator=mock_validator)

        captured = {}

        async def fake_handle(ws, room_id, peer_id):
            captured["peer_id"] = peer_id
            # Normal return — no raise needed

        broker.handle_connection = fake_handle

        ws = _mock_ws()
        ws.headers = MagicMock()
        ws.headers.get = MagicMock(return_value=None)

        await signaling_ws_endpoint(ws=ws, room=ROOM_ID, peer=PEER_B, broker=broker)

        # When auth returns "anonymous", the peer param is used
        assert captured.get("peer_id") == PEER_B
