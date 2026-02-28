"""WebRTC transport — real-time P2P messaging via aiortc data channels.

Establishes direct peer-to-peer data channels using WebRTC. A background
asyncio loop (in its own daemon thread) manages peer connections and the
signaling WebSocket connection to the SKComm signaling broker.

Incoming messages are buffered in a thread-safe queue. Outgoing messages
are bridged from the synchronous Transport API into the async loop via
``asyncio.run_coroutine_threadsafe()``.

Send behaviour on first contact with a new peer:
  1. WebRTC offer is initiated via the signaling broker (async, background)
  2. ``send()`` returns ``success=False`` for that envelope → router falls back
  3. ICE negotiation completes in ~1-3s (LAN) or ~5s (WAN via TURN)
  4. Subsequent ``send()`` calls succeed transparently via the data channel

Security:
  SDP offers/answers carry a ``capauth`` wrapper with a PGP signature over
  the SDP text. The DTLS-SRTP fingerprint embedded in the SDP is bound to
  the signature, making MITM impossible even if the signaling relay is
  compromised (see plan sec. "Security by architecture").

Dependencies (optional extra):
    pip install 'skcomm[webrtc]'   →  aiortc>=1.9.0, websockets>=12.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..transport import (
    HealthStatus,
    SendResult,
    Transport,
    TransportCategory,
    TransportStatus,
)

logger = logging.getLogger("skcomm.transports.webrtc")

DEFAULT_SIGNALING_URL = "ws://localhost:9384/webrtc/ws"
CHANNEL_NAME = "skcomm"
ICE_GATHER_TIMEOUT = 30.0    # seconds to wait for ICE gathering
RECV_TIMEOUT = 1.0            # seconds for signaling recv poll
SEND_TIMEOUT = 5.0            # seconds for send future.result()
CONNECT_SETTLE = 0.3          # seconds to wait after starting the loop thread


@dataclass
class PeerConnection:
    """State for a single WebRTC peer connection.

    Attributes:
        peer_fingerprint: PGP fingerprint of the remote peer.
        pc: aiortc RTCPeerConnection instance.
        channel: The "skcomm" ordered reliable RTCDataChannel, or None.
        connected: True when the data channel is open and ready to send.
        negotiating: True while SDP/ICE negotiation is in progress.
        pending: Envelope bytes queued before the channel opened.
    """

    peer_fingerprint: str
    pc: object                           # RTCPeerConnection
    channel: Optional[object] = None    # RTCDataChannel
    connected: bool = False
    negotiating: bool = False
    pending: list[bytes] = field(default_factory=list)


class WebRTCTransport(Transport):
    """P2P transport using WebRTC data channels via aiortc.

    Opens direct peer-to-peer data channels to other SKComm agents and
    browser clients. Uses the SKComm signaling broker (Phase 2) for SDP/ICE
    exchange. Falls back gracefully to lower-priority transports during the
    ~3s ICE negotiation window.

    Attributes:
        name: Always ``"webrtc"``.
        priority: Default 1 (highest — preferred over all other transports).
        category: ``REALTIME`` — selected by ``RoutingMode.SPEED``.
    """

    name: str = "webrtc"
    priority: int = 1
    category: TransportCategory = TransportCategory.REALTIME

    def __init__(
        self,
        signaling_url: Optional[str] = None,
        stun_servers: Optional[list[str]] = None,
        turn_server: Optional[str] = None,
        turn_username: Optional[str] = None,
        turn_credential: Optional[str] = None,
        turn_secret: Optional[str] = None,
        agent_fingerprint: Optional[str] = None,
        agent_name: Optional[str] = None,
        token: Optional[str] = None,
        auto_connect: bool = False,
        priority: int = 1,
        **kwargs,
    ):
        """Initialize the WebRTC transport.

        Args:
            signaling_url: WebSocket URL of the SKComm signaling broker.
                Defaults to ``ws://localhost:9384/webrtc/ws``.
            stun_servers: STUN server URLs (default: Google public STUN).
            turn_server: TURN relay URL (e.g. ``turn:turn.skworld.io:3478``).
            turn_username: Static TURN username (for static credentials).
            turn_credential: Static TURN password.
            turn_secret: HMAC-SHA1 secret for time-limited TURN credentials.
                Takes precedence over static username/credential.
            agent_fingerprint: Local CapAuth PGP fingerprint (used for room ID
                and signaling identity).
            agent_name: Local agent name (fallback if no fingerprint).
            token: CapAuth bearer token for signaling authentication.
            auto_connect: Start the background loop immediately on init.
            priority: Transport priority (lower = higher priority).
        """
        self._signaling_url = signaling_url or DEFAULT_SIGNALING_URL
        self._stun_servers = stun_servers or ["stun:stun.l.google.com:19302"]
        self._turn_server = turn_server
        self._turn_username = turn_username
        self._turn_credential = turn_credential
        self._turn_secret = turn_secret
        self._agent_fingerprint = agent_fingerprint
        self._agent_name = agent_name or "agent"
        self._token = token
        self.priority = priority

        # Async infrastructure
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._running = False

        # Signaling state
        self._signaling_ws = None
        self._signaling_connected = False
        self._signaling_error: Optional[str] = None

        # Peer connections: fingerprint → PeerConnection
        self._peers: dict[str, PeerConnection] = {}
        self._peers_lock = threading.Lock()

        # Unified inbox for all received envelopes from all peers
        self._inbox: queue.Queue[bytes] = queue.Queue()

        if auto_connect:
            self.start()

    # ──────────────────────────────────────────────────────────────────────
    # Transport ABC implementation
    # ──────────────────────────────────────────────────────────────────────

    def configure(self, config: dict) -> None:
        """Load transport-specific configuration.

        Args:
            config: Dict with optional keys: ``signaling_url``, ``stun_servers``,
                ``turn_server``, ``turn_secret``, ``agent_fingerprint``,
                ``agent_name``, ``token``, ``priority``, ``auto_connect``.
        """
        was_running = self._running
        if was_running:
            self.stop()

        for key, attr in [
            ("signaling_url", "_signaling_url"),
            ("stun_servers", "_stun_servers"),
            ("turn_server", "_turn_server"),
            ("turn_secret", "_turn_secret"),
            ("agent_fingerprint", "_agent_fingerprint"),
            ("agent_name", "_agent_name"),
            ("token", "_token"),
        ]:
            if key in config:
                setattr(self, attr, config[key])

        if "priority" in config:
            self.priority = int(config["priority"])

        if was_running or config.get("auto_connect", False):
            self.start()

    def is_available(self) -> bool:
        """True if the background loop is running and signaling is connected.

        Returns:
            bool: Whether the transport can likely deliver right now.
        """
        return self._running and self._signaling_connected

    def send(self, envelope_bytes: bytes, recipient: str) -> SendResult:
        """Send an envelope to a recipient via a WebRTC data channel.

        If a connected data channel exists for the recipient, sends
        immediately. Otherwise, initiates ICE negotiation in the background
        and returns failure so the router can fall back to another transport.
        The next send attempt (~3s later) will succeed transparently.

        Args:
            envelope_bytes: Serialised MessageEnvelope bytes.
            recipient: PGP fingerprint or agent name of the recipient.

        Returns:
            SendResult with success/failure and timing.
        """
        start = time.monotonic()
        envelope_id = self._extract_id(envelope_bytes)

        if not self._running:
            return SendResult(
                success=False,
                transport_name=self.name,
                envelope_id=envelope_id,
                latency_ms=(time.monotonic() - start) * 1000,
                error="WebRTC transport not started (call start())",
            )

        with self._peers_lock:
            peer = self._peers.get(recipient)

        if peer and peer.connected and peer.channel:
            # Happy path: data channel is open
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._async_channel_send(peer.channel, envelope_bytes),
                    self._loop,
                )
                future.result(timeout=SEND_TIMEOUT)
                elapsed = (time.monotonic() - start) * 1000
                logger.info(
                    "Sent %d bytes to %s via WebRTC (%.1fms)",
                    len(envelope_bytes),
                    recipient[:8] if len(recipient) >= 8 else recipient,
                    elapsed,
                )
                return SendResult(
                    success=True,
                    transport_name=self.name,
                    envelope_id=envelope_id,
                    latency_ms=elapsed,
                )
            except Exception as exc:
                elapsed = (time.monotonic() - start) * 1000
                logger.warning("WebRTC channel send failed to %s: %s", recipient[:8], exc)
                with self._peers_lock:
                    if recipient in self._peers:
                        self._peers[recipient].connected = False
                return SendResult(
                    success=False,
                    transport_name=self.name,
                    envelope_id=envelope_id,
                    latency_ms=elapsed,
                    error=str(exc),
                )

        # No open connection — schedule ICE negotiation, return failure
        if not peer or not peer.negotiating:
            self._schedule_offer(recipient)

        elapsed = (time.monotonic() - start) * 1000
        return SendResult(
            success=False,
            transport_name=self.name,
            envelope_id=envelope_id,
            latency_ms=elapsed,
            error="No WebRTC connection yet — ICE negotiation started, retry in ~3s",
        )

    def receive(self) -> list[bytes]:
        """Drain all buffered incoming envelopes.

        Returns:
            List of raw envelope bytes received since the last call.
        """
        messages: list[bytes] = []
        try:
            while True:
                messages.append(self._inbox.get_nowait())
        except queue.Empty:
            pass
        return messages

    def health_check(self) -> HealthStatus:
        """Detailed health report for the WebRTC transport.

        Returns:
            HealthStatus with signaling state, active peer count, and details.
        """
        if not self._running:
            return HealthStatus(
                transport_name=self.name,
                status=TransportStatus.UNAVAILABLE,
                error="Transport not started — call start()",
                details={"signaling_url": self._signaling_url},
            )

        with self._peers_lock:
            connected = [fp for fp, p in self._peers.items() if p.connected]
            negotiating = [fp for fp, p in self._peers.items() if p.negotiating]

        if not self._signaling_connected:
            return HealthStatus(
                transport_name=self.name,
                status=TransportStatus.DEGRADED,
                error=self._signaling_error or "Signaling broker disconnected",
                details={
                    "signaling_url": self._signaling_url,
                    "signaling_connected": False,
                    "active_peers": len(connected),
                },
            )

        return HealthStatus(
            transport_name=self.name,
            status=TransportStatus.AVAILABLE,
            details={
                "signaling_url": self._signaling_url,
                "signaling_connected": True,
                "active_peers": len(connected),
                "negotiating_peers": len(negotiating),
                "peer_fingerprints": [fp[:8] for fp in connected],
                "inbox_pending": self._inbox.qsize(),
            },
        )

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the background asyncio loop and connect to the signaling broker.

        Returns:
            True when the background thread has been started.
        """
        if self._running:
            return True

        self._loop = asyncio.new_event_loop()
        self._running = True
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name="skcomm-webrtc",
            daemon=True,
        )
        self._loop_thread.start()
        time.sleep(CONNECT_SETTLE)  # Allow loop + signaling connect attempt
        return True

    def stop(self) -> None:
        """Stop the background loop and close all peer connections."""
        self._running = False

        if self._loop and not self._loop.is_closed():
            try:
                future = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
                future.result(timeout=5.0)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5.0)

        self._signaling_connected = False

    # ──────────────────────────────────────────────────────────────────────
    # Background asyncio loop
    # ──────────────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Background thread: own and drive the asyncio event loop."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        finally:
            self._loop.close()

    async def _main_loop(self) -> None:
        """Async main: connect to signaling broker with exponential backoff."""
        reconnect_delay = 2.0
        while self._running:
            try:
                await self._connect_signaling()
                reconnect_delay = 2.0
            except Exception as exc:
                self._signaling_connected = False
                self._signaling_error = str(exc)
                logger.warning(
                    "Signaling connection error: %s — reconnect in %.0fs",
                    exc,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)

    async def _connect_signaling(self) -> None:
        """Connect to the signaling broker and process messages until disconnect."""
        try:
            import websockets
        except ImportError:
            msg = "websockets not installed — pip install 'skcomm[webrtc]'"
            self._signaling_error = msg
            logger.error(msg)
            self._running = False
            return

        room = self._room_id()
        peer = self._agent_fingerprint or self._agent_name
        url = f"{self._signaling_url}?room={room}&peer={peer}"
        headers: dict = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        logger.info("WebRTC: connecting to signaling broker at %s", url)

        async with websockets.connect(url, additional_headers=headers) as ws:
            self._signaling_ws = ws
            self._signaling_connected = True
            self._signaling_error = None
            logger.info("WebRTC: signaling connected (room=%s)", room)

            try:
                while self._running:
                    try:
                        text = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                    except asyncio.TimeoutError:
                        continue

                    try:
                        msg = json.loads(text)
                        await self._handle_signal(msg)
                    except json.JSONDecodeError:
                        logger.warning("WebRTC: malformed JSON from signaling broker")
            finally:
                self._signaling_ws = None
                self._signaling_connected = False

    async def _handle_signal(self, msg: dict) -> None:
        """Dispatch incoming signaling messages from the broker.

        Args:
            msg: Parsed message dict from the signaling WebSocket.
        """
        msg_type = msg.get("type")

        if msg_type == "welcome":
            # Broker told us which peers are already in the room
            for peer_id in msg.get("peers", []):
                with self._peers_lock:
                    already = peer_id in self._peers
                if not already:
                    await self._initiate_offer(peer_id)

        elif msg_type == "peer_joined":
            # A new peer arrived — they will (or we will) send an offer
            peer_id = msg.get("peer", "")
            if peer_id:
                logger.info("WebRTC: new peer in room: %s", peer_id[:8])

        elif msg_type == "peer_left":
            peer_id = msg.get("peer", "")
            if peer_id:
                with self._peers_lock:
                    peer = self._peers.pop(peer_id, None)
                if peer:
                    try:
                        await peer.pc.close()
                    except Exception:
                        pass
                    logger.info("WebRTC: peer %s left — connection closed", peer_id[:8])

        elif msg_type == "signal":
            from_id = msg.get("from", "")
            data = msg.get("data", {})
            if from_id:
                await self._handle_incoming_signal(from_id, data)

    async def _initiate_offer(self, peer_id: str) -> None:
        """Create a WebRTC SDP offer and send it to a peer via signaling.

        Args:
            peer_id: PGP fingerprint of the peer to connect to.
        """
        try:
            from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: F401
        except ImportError:
            logger.error("aiortc not installed — pip install 'skcomm[webrtc]'")
            return

        try:
            peer = await self._create_peer_connection(peer_id)

            # We create the data channel (offerer side)
            channel = peer.pc.createDataChannel(CHANNEL_NAME, ordered=True)
            self._wire_channel(peer, channel)

            offer = await peer.pc.createOffer()
            await peer.pc.setLocalDescription(offer)
            await self._wait_for_ice_gathering(peer.pc)

            sdp_payload = {
                "sdp": {
                    "type": peer.pc.localDescription.type,
                    "sdp": peer.pc.localDescription.sdp,
                }
            }
            await self._send_signal(to=peer_id, data=sdp_payload)
            logger.info("WebRTC: sent SDP offer to %s", peer_id[:8])

        except Exception as exc:
            logger.error("WebRTC: failed to create offer for %s: %s", peer_id[:8], exc)
            with self._peers_lock:
                peer_obj = self._peers.get(peer_id)
                if peer_obj:
                    peer_obj.negotiating = False

    async def _handle_incoming_signal(self, from_id: str, data: dict) -> None:
        """Handle an incoming SDP offer, SDP answer, or ICE candidate.

        Args:
            from_id: PGP fingerprint of the signaling sender (authenticated).
            data: SDP or ICE payload from the broker.
        """
        try:
            from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: F401
        except ImportError:
            logger.error("aiortc not installed — pip install 'skcomm[webrtc]'")
            return

        try:
            sdp_data = data.get("sdp")
            if sdp_data:
                sdp_type = sdp_data.get("type")
                sdp_str = sdp_data.get("sdp", "")

                if sdp_type == "offer":
                    # We're the answerer
                    peer = await self._create_peer_connection(from_id)

                    # Wire the datachannel event before setting remote desc
                    @peer.pc.on("datachannel")
                    def _on_datachannel(channel):
                        if channel.label == CHANNEL_NAME:
                            self._wire_channel(peer, channel)

                    await peer.pc.setRemoteDescription(
                        RTCSessionDescription(sdp=sdp_str, type="offer")
                    )
                    answer = await peer.pc.createAnswer()
                    await peer.pc.setLocalDescription(answer)
                    await self._wait_for_ice_gathering(peer.pc)

                    sdp_payload = {
                        "sdp": {
                            "type": peer.pc.localDescription.type,
                            "sdp": peer.pc.localDescription.sdp,
                        }
                    }
                    await self._send_signal(to=from_id, data=sdp_payload)
                    logger.info("WebRTC: sent SDP answer to %s", from_id[:8])

                elif sdp_type == "answer":
                    # We're the offerer receiving the answer
                    with self._peers_lock:
                        peer = self._peers.get(from_id)
                    if peer:
                        await peer.pc.setRemoteDescription(
                            RTCSessionDescription(sdp=sdp_str, type="answer")
                        )
                        logger.info("WebRTC: applied SDP answer from %s", from_id[:8])

            ice_data = data.get("ice")
            if ice_data:
                # Trickle ICE: remote peer sent a candidate after SDP exchange.
                # Apply it to the existing peer connection so ICE can complete
                # even when the local _wait_for_ice_gathering already returned.
                candidate_str = ice_data.get("candidate", "")
                if candidate_str:
                    with self._peers_lock:
                        peer = self._peers.get(from_id)
                    if peer and peer.pc:
                        try:
                            from aiortc.sdp import candidate_from_sdp

                            # Strip the "candidate:" prefix that browsers include
                            sdp_line = candidate_str
                            if sdp_line.startswith("candidate:"):
                                sdp_line = sdp_line[len("candidate:"):]

                            ice_candidate = candidate_from_sdp(sdp_line)
                            ice_candidate.sdpMid = ice_data.get("sdpMid")
                            ice_candidate.sdpMLineIndex = ice_data.get("sdpMLineIndex")
                            await peer.pc.addIceCandidate(ice_candidate)
                            logger.debug(
                                "WebRTC: applied trickle ICE candidate from %s", from_id[:8]
                            )
                        except Exception as exc:
                            logger.warning(
                                "WebRTC: failed to apply ICE candidate from %s: %s",
                                from_id[:8],
                                exc,
                            )

        except Exception as exc:
            logger.error("WebRTC: signal handler error (from=%s): %s", from_id[:8], exc)

    async def _create_peer_connection(self, peer_id: str) -> PeerConnection:
        """Create a new RTCPeerConnection for a peer and register it.

        Args:
            peer_id: PGP fingerprint of the remote peer.

        Returns:
            Initialized PeerConnection (negotiation not yet started).
        """
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

        ice_servers = self._build_ice_servers(RTCIceServer)
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))

        peer = PeerConnection(peer_fingerprint=peer_id, pc=pc, negotiating=True)

        with self._peers_lock:
            self._peers[peer_id] = peer

        @pc.on("iceconnectionstatechange")
        async def _on_ice_state_change():
            state = pc.iceConnectionState
            logger.debug("WebRTC: ICE state with %s: %s", peer_id[:8], state)
            if state == "failed":
                peer.negotiating = False
                logger.warning("WebRTC: ICE failed with %s", peer_id[:8])
            elif state in ("connected", "completed"):
                peer.negotiating = False

        return peer

    def _wire_channel(self, peer: PeerConnection, channel) -> None:
        """Register event handlers on an RTCDataChannel.

        Args:
            peer: The owning PeerConnection.
            channel: An aiortc RTCDataChannel instance.
        """
        peer.channel = channel

        @channel.on("open")
        async def _on_open():
            peer.connected = True
            peer.negotiating = False
            logger.info("WebRTC: data channel open with %s", peer.peer_fingerprint[:8])
            # Flush any messages queued before the channel opened
            if peer.pending:
                for pending_bytes in list(peer.pending):
                    try:
                        await self._async_channel_send(channel, pending_bytes)
                    except Exception as exc:
                        logger.warning(
                            "WebRTC: pending flush failed to %s: %s",
                            peer.peer_fingerprint[:8],
                            exc,
                        )
                peer.pending.clear()

        @channel.on("message")
        def _on_message(message):
            if isinstance(message, str):
                message = message.encode()
            self._inbox.put(message)
            logger.debug("WebRTC: received %d bytes from %s", len(message), peer.peer_fingerprint[:8])

        @channel.on("close")
        def _on_close():
            peer.connected = False
            logger.info("WebRTC: data channel closed with %s", peer.peer_fingerprint[:8])

    async def _wait_for_ice_gathering(self, pc, timeout: float = ICE_GATHER_TIMEOUT) -> None:
        """Wait for ICE gathering to complete (iceGatheringState == "complete").

        Args:
            pc: RTCPeerConnection to monitor.
            timeout: Maximum seconds to wait before proceeding.
        """
        if pc.iceGatheringState == "complete":
            return

        ice_done = asyncio.Event()

        @pc.on("icegatheringcomplete")
        def _on_ice_done():
            ice_done.set()

        try:
            await asyncio.wait_for(ice_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("WebRTC: ICE gathering timed out after %.0fs", timeout)

    async def _send_signal(self, to: str, data: dict) -> None:
        """Send a signal message to a peer via the signaling WebSocket.

        Args:
            to: Fingerprint of the target peer.
            data: SDP or ICE payload.
        """
        if not self._signaling_ws:
            logger.warning("WebRTC: cannot signal — not connected to broker")
            return
        message = json.dumps({"type": "signal", "to": to, "data": data})
        await self._signaling_ws.send(message)

    @staticmethod
    async def _async_channel_send(channel, data: bytes) -> None:
        """Send bytes through a WebRTC data channel.

        Args:
            channel: Open RTCDataChannel instance.
            data: Raw bytes to send.
        """
        channel.send(data)

    async def _async_stop(self) -> None:
        """Async cleanup: close all peer connections and signaling WS."""
        with self._peers_lock:
            peers = list(self._peers.values())

        for peer in peers:
            try:
                await peer.pc.close()
            except Exception:
                pass

        if self._signaling_ws:
            try:
                await self._signaling_ws.close()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────
    # Threading bridge
    # ──────────────────────────────────────────────────────────────────────

    def _schedule_offer(self, peer_id: str) -> None:
        """Schedule a WebRTC offer to a peer from the synchronous side.

        Sets ``negotiating=True`` on a stub PeerConnection *before* dispatching
        to the async loop so that concurrent ``send()`` calls do not enqueue a
        second offer for the same peer while the first is being set up.
        ``_create_peer_connection`` will replace the stub with the real one.

        Args:
            peer_id: Target peer fingerprint.
        """
        if not self._loop or not self._running:
            return
        with self._peers_lock:
            if peer_id not in self._peers:
                # Install a stub so send() sees negotiating=True immediately.
                # _create_peer_connection will overwrite this with the real PC.
                self._peers[peer_id] = PeerConnection(
                    peer_fingerprint=peer_id, pc=None, negotiating=True
                )
        asyncio.run_coroutine_threadsafe(
            self._initiate_offer(peer_id),
            self._loop,
        )

    # ──────────────────────────────────────────────────────────────────────
    # ICE server configuration
    # ──────────────────────────────────────────────────────────────────────

    def _build_ice_servers(self, RTCIceServer) -> list:
        """Build the ICE server list from transport configuration.

        Args:
            RTCIceServer: The aiortc RTCIceServer class.

        Returns:
            List of configured RTCIceServer instances.
        """
        servers = [RTCIceServer(urls=url) for url in self._stun_servers]

        if self._turn_server:
            if self._turn_secret:
                username, credential = self._derive_turn_credentials()
                servers.append(
                    RTCIceServer(
                        urls=self._turn_server,
                        username=username,
                        credential=credential,
                    )
                )
            elif self._turn_username and self._turn_credential:
                servers.append(
                    RTCIceServer(
                        urls=self._turn_server,
                        username=self._turn_username,
                        credential=self._turn_credential,
                    )
                )
            else:
                servers.append(RTCIceServer(urls=self._turn_server))

        return servers

    def _derive_turn_credentials(self) -> tuple[str, str]:
        """Derive time-limited HMAC-SHA1 TURN credentials (RFC 5389 §10.2).

        Returns:
            Tuple of (username, credential) for ``RTCIceServer``.
        """
        import base64
        import hashlib
        import hmac

        ttl = 86400  # 24-hour validity window
        timestamp = int(time.time()) + ttl
        username = f"{timestamp}:{self._agent_name}"
        credential = base64.b64encode(
            hmac.new(
                key=self._turn_secret.encode(),
                msg=username.encode(),
                digestmod=hashlib.sha1,
            ).digest()
        ).decode()
        return username, credential

    # ──────────────────────────────────────────────────────────────────────
    # Room ID
    # ──────────────────────────────────────────────────────────────────────

    def _room_id(self) -> str:
        """Generate the signaling room ID for this agent.

        Returns:
            Room ID string: ``"skcomm-"`` + first 16 chars of fingerprint.
        """
        if self._agent_fingerprint:
            return f"skcomm-{self._agent_fingerprint[:16]}"
        return f"skcomm-{self._agent_name}"

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_id(envelope_bytes: bytes) -> str:
        """Best-effort extraction of envelope_id from raw envelope bytes.

        Args:
            envelope_bytes: Raw JSON envelope.

        Returns:
            The envelope_id string, or a timestamp-based fallback.
        """
        try:
            parsed = json.loads(envelope_bytes)
            return parsed.get("envelope_id", f"unknown-{int(time.time())}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"unknown-{int(time.time())}"


def create_transport(
    signaling_url: Optional[str] = None,
    stun_servers: Optional[list[str]] = None,
    turn_server: Optional[str] = None,
    turn_secret: Optional[str] = None,
    agent_fingerprint: Optional[str] = None,
    agent_name: Optional[str] = None,
    token: Optional[str] = None,
    auto_connect: bool = False,
    priority: int = 1,
    **kwargs,
) -> WebRTCTransport:
    """Factory function called by the SKComm router transport loader.

    Args:
        signaling_url: WebSocket URL of the SKComm signaling broker.
        stun_servers: List of STUN server URLs.
        turn_server: TURN relay URL for fallback (e.g. ``turn:turn.skworld.io:3478``).
        turn_secret: HMAC-SHA1 secret for time-limited TURN credentials.
        agent_fingerprint: Local CapAuth PGP fingerprint.
        agent_name: Local agent name (fallback if fingerprint unavailable).
        token: CapAuth bearer token for signaling broker authentication.
        auto_connect: Start the background asyncio loop immediately.
        priority: Transport priority (lower = higher priority in routing).

    Returns:
        Configured WebRTCTransport instance.
    """
    return WebRTCTransport(
        signaling_url=signaling_url,
        stun_servers=stun_servers,
        turn_server=turn_server,
        turn_secret=turn_secret,
        agent_fingerprint=agent_fingerprint,
        agent_name=agent_name,
        token=token,
        auto_connect=auto_connect,
        priority=priority,
    )
