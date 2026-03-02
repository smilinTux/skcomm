"""
SKComm REST API — FastAPI server wrapping the SKComm Python API.

Provides HTTP endpoints for Flutter/desktop clients to send and receive
messages through SKComm without requiring Python bindings.

Run standalone:
    uvicorn skcomm.api:app --host 127.0.0.1 --port 9384

Run from CLI:
    skcomm serve --host 127.0.0.1 --port 9384
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

from .capauth_validator import CapAuthValidator
from .core import SKComm
from .discovery import PeerInfo, PeerStore
from .heartbeat import HeartbeatConfig, HeartbeatPublisher
from .models import MessageEnvelope, MessageType, RoutingMode, Urgency
from .outbox import PersistentOutbox
from .signaling import SignalingBroker, signaling_ws_endpoint

logger = logging.getLogger("skcomm.api")

# Global SKComm instance (initialized on startup)
_skcomm: Optional[SKComm] = None

# Global WebRTC signaling broker (initialized on startup)
_broker: Optional[SignalingBroker] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage SKComm lifecycle on server startup/shutdown."""
    global _skcomm, _broker
    logger.info("Starting SKComm API server...")
    try:
        _skcomm = SKComm.from_config()
        logger.info(
            "SKComm initialized as '%s' with %d transports",
            _skcomm.identity,
            len(_skcomm.router.transports),
        )
    except Exception as exc:
        logger.exception("Failed to initialize SKComm")
        raise

    # Initialize the WebRTC signaling broker.
    # require_auth=True (default) enforces PGP token verification on every
    # WebSocket upgrade. To disable auth during local development set
    # SKCOMM_DEV_AUTH=1 in the environment — this flips require_auth=False
    # which accepts plain 40-hex fingerprints with no signature check.
    import os as _os
    import sys as _sys

    # SECURITY: SKCOMM_DEV_AUTH disables CapAuth PGP signature verification
    # on WebSocket signaling connections. This means ANY 40-hex string is
    # accepted as a valid fingerprint with NO cryptographic proof of identity.
    # An attacker on the same network can impersonate any agent.
    # Accepted values: "1", "true", "yes", "i_know_what_im_doing"
    # This MUST NEVER be set in production deployments.
    dev_auth_val = _os.environ.get("SKCOMM_DEV_AUTH", "").lower()
    dev_auth = dev_auth_val in {"1", "true", "yes", "i_know_what_im_doing"}
    if dev_auth:
        _dev_banner = (
            "\n"
            "========================================================\n"
            "  WARNING: SKCOMM_DEV_AUTH is SET -- AUTH DISABLED\n"
            "\n"
            "  CapAuth PGP signature verification is OFF.\n"
            "  Any 40-hex string is accepted as a valid fingerprint.\n"
            "  An attacker on the network can impersonate any agent.\n"
            "\n"
            "  DO NOT run with this setting in production.\n"
            "========================================================\n"
        )
        print(_dev_banner, file=_sys.stderr, flush=True)
        logger.warning(
            "WebRTC signaling: SKCOMM_DEV_AUTH=%s -- CapAuth signature check DISABLED. "
            "Do NOT use in production.",
            dev_auth_val,
        )
    _broker = SignalingBroker(
        validator=CapAuthValidator(require_auth=not dev_auth),
    )
    logger.info("WebRTC signaling broker initialized (require_auth=%s)", not dev_auth)

    yield

    logger.info("Shutting down SKComm API server...")
    _skcomm = None
    _broker = None


app = FastAPI(
    title="SKComm API",
    description="Transport-agnostic encrypted communication for sovereign AI",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class _PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    """Return Access-Control-Allow-Private-Network: true on PNA preflights.

    Chrome/Brave enforce the Private Network Access spec: when an extension
    service worker fetches a localhost URL it first sends an OPTIONS preflight
    with Access-Control-Request-Private-Network: true.  FastAPI's CORS
    middleware doesn't handle this header, so it returns 400.  This wrapper
    intercepts those preflights and approves them.
    """

    async def dispatch(self, request, call_next):
        if (
            request.method == "OPTIONS"
            and request.headers.get("access-control-request-private-network") == "true"
        ):
            response = await call_next(request)
            response.headers["Access-Control-Allow-Private-Network"] = "true"
            if response.status_code >= 400:
                response.status_code = 204
            return response
        return await call_next(request)


app.add_middleware(_PrivateNetworkAccessMiddleware)


def get_skcomm() -> SKComm:
    """Get or create the global SKComm instance.

    Returns:
        Configured SKComm instance.

    Raises:
        HTTPException: If SKComm initialization fails.
    """
    global _skcomm
    if _skcomm is None:
        try:
            _skcomm = SKComm.from_config()
            logger.info(
                "SKComm initialized as '%s' with %d transports",
                _skcomm.identity,
                len(_skcomm.router.transports),
            )
        except Exception as exc:
            logger.exception("Failed to initialize SKComm")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to initialize SKComm: {exc}",
            ) from exc
    return _skcomm


# Peer name: alphanumeric, hyphens, underscores. 1-64 chars.
_PEER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _validate_peer_name(name: str) -> str:
    """Validate a peer name for safe use in file paths.

    Rejects names containing path traversal sequences or characters that
    could escape the peers directory.

    Args:
        name: Raw peer name from the request.

    Returns:
        The validated name (unchanged).

    Raises:
        HTTPException: 400 if the name is invalid.
    """
    if not _PEER_NAME_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid peer name '{name}': must be 1-64 alphanumeric "
                "characters, hyphens, or underscores, starting with an "
                "alphanumeric character"
            ),
        )
    return name


class SendMessageRequest(BaseModel):
    """Request body for POST /api/v1/send."""

    recipient: str = Field(
        ...,
        description="Agent name or PGP fingerprint of the recipient",
        examples=["lumina", "opus"],
    )
    message: str = Field(
        ...,
        description="The message content (plaintext)",
        examples=["Hello from the SKComm API!"],
    )
    message_type: MessageType = Field(
        default=MessageType.TEXT,
        description="Type of content being sent",
    )
    mode: Optional[RoutingMode] = Field(
        default=None,
        description="Override the default routing mode",
    )
    thread_id: Optional[str] = Field(
        default=None,
        description="Optional conversation thread ID",
    )
    in_reply_to: Optional[str] = Field(
        default=None,
        description="Optional envelope_id this is a reply to",
    )
    urgency: Urgency = Field(
        default=Urgency.NORMAL,
        description="Message urgency level",
    )


class SendMessageResponse(BaseModel):
    """Response body for POST /api/v1/send."""

    delivered: bool = Field(
        ...,
        description="Whether the message was successfully delivered",
    )
    envelope_id: str = Field(
        ...,
        description="Unique identifier for the sent message",
    )
    transport_used: Optional[str] = Field(
        default=None,
        description="Name of the transport that delivered the message",
    )
    attempts: list[dict] = Field(
        default_factory=list,
        description="List of delivery attempts with transport names and results",
    )


class MessageEnvelopeResponse(BaseModel):
    """Response model for received messages."""

    envelope_id: str
    sender: str
    recipient: str
    content: str
    content_type: MessageType
    encrypted: bool
    compressed: bool
    signature: Optional[str] = None
    thread_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    urgency: Urgency
    created_at: datetime
    is_ack: bool


class ConversationResponse(BaseModel):
    """Response model for conversations."""

    thread_id: str
    participants: list[str]
    message_count: int
    last_message_at: datetime
    last_message_preview: str


class AgentResponse(BaseModel):
    """Response model for known agents."""

    name: str
    fingerprint: Optional[str] = None
    last_seen: Optional[datetime] = None
    message_count: int


class PeerTransportResponse(BaseModel):
    """Response model for a single peer transport entry."""

    transport: str
    settings: dict


class PeerResponse(BaseModel):
    """Response model for a peer directory entry."""

    name: str
    fingerprint: Optional[str] = None
    nostr_pubkey: Optional[str] = None
    transports: list[PeerTransportResponse] = []
    discovered_via: str
    last_seen: Optional[datetime] = None


class PeerAddRequest(BaseModel):
    """Request body for POST /api/v1/peers."""

    name: str = Field(..., description="Friendly agent name (e.g. 'lumina')")
    address: str = Field(
        ...,
        description="Transport address or URI (e.g. syncthing folder path, skcomm://...)",
    )
    transport: str = Field(
        default="syncthing",
        description="Transport type: syncthing, file, nostr, etc.",
    )
    fingerprint: Optional[str] = Field(
        default=None,
        description="PGP fingerprint for this peer",
    )


class PresenceRequest(BaseModel):
    """Request body for POST /api/v1/presence."""

    status: str = Field(
        ...,
        description="Presence status (e.g., 'online', 'away', 'busy')",
        examples=["online", "away", "busy"],
    )
    message: Optional[str] = Field(
        default=None,
        description="Optional status message",
        examples=["Working on SKComm API"],
    )


@app.get("/", tags=["health"])
async def root():
    """Root endpoint — health check."""
    return {
        "service": "SKComm API",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/api/v1/status", tags=["status"])
async def get_status():
    """Get the current status of SKComm.

    Returns:
        Dict with identity, transport health, crypto state, and config summary.
    """
    comm = get_skcomm()
    return comm.status()


@app.post(
    "/api/v1/send",
    response_model=SendMessageResponse,
    status_code=status.HTTP_200_OK,
    tags=["messaging"],
)
async def send_message(request: SendMessageRequest):
    """Send a message to a recipient.

    Creates an envelope, routes it through available transports.

    Args:
        request: SendMessageRequest with message details.

    Returns:
        SendMessageResponse with delivery status and envelope ID.

    Raises:
        HTTPException: If message sending fails completely.
    """
    comm = get_skcomm()

    try:
        report = comm.send(
            recipient=request.recipient,
            message=request.message,
            message_type=request.message_type,
            mode=request.mode,
            thread_id=request.thread_id,
            in_reply_to=request.in_reply_to,
            urgency=request.urgency,
        )

        attempts = [
            {
                "transport": attempt.transport_name,
                "success": attempt.success,
                "latency_ms": attempt.latency_ms,
                "error": attempt.error,
            }
            for attempt in report.attempts
        ]

        return SendMessageResponse(
            delivered=report.delivered,
            envelope_id=report.envelope_id,
            transport_used=report.successful_transport if report.delivered else None,
            attempts=attempts,
        )

    except Exception as exc:
        logger.exception("Failed to send message")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send message: {exc}",
        ) from exc


@app.get(
    "/api/v1/inbox",
    response_model=list[MessageEnvelopeResponse],
    tags=["messaging"],
)
async def get_inbox():
    """Check all transports for incoming messages.

    Polls every available transport, deduplicates, and deserializes.

    Returns:
        List of received MessageEnvelope objects.
    """
    comm = get_skcomm()

    try:
        envelopes = comm.receive()

        return [
            MessageEnvelopeResponse(
                envelope_id=env.envelope_id,
                sender=env.sender,
                recipient=env.recipient,
                content=env.payload.content,
                content_type=env.payload.content_type,
                encrypted=env.payload.encrypted,
                compressed=env.payload.compressed,
                signature=env.payload.signature,
                thread_id=env.metadata.thread_id,
                in_reply_to=env.metadata.in_reply_to,
                urgency=env.metadata.urgency,
                created_at=env.metadata.created_at,
                is_ack=env.is_ack,
            )
            for env in envelopes
        ]

    except Exception as exc:
        logger.exception("Failed to retrieve inbox")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve inbox: {exc}",
        ) from exc


@app.get(
    "/api/v1/conversations",
    response_model=list[ConversationResponse],
    tags=["messaging"],
)
async def get_conversations():
    """Get a list of active conversations.

    Groups messages by thread_id (or sender:recipient pair) and returns
    conversation metadata. Sources:
    1. Persistent outbox (pending + dead-letter queues)
    2. Syncthing comms outbox/inbox folders (delivered messages)

    Returns:
        List of ConversationResponse objects, newest first.
    """
    import json as _json
    import os as _os

    # ── Source 1: Persistent outbox (retry queue) ────────────────────
    try:
        outbox = PersistentOutbox()
        all_entries = outbox.list_pending() + outbox.list_dead()
    except Exception as exc:
        logger.warning("Could not read outbox for conversations: %s", exc)
        all_entries = []

    # Group envelopes by thread_id, falling back to "sender:recipient"
    threads: dict[str, dict] = defaultdict(lambda: {
        "participants": set(),
        "count": 0,
        "last_at": None,
        "preview": "",
    })

    for entry in all_entries:
        try:
            env = MessageEnvelope.from_bytes(entry.envelope_json.encode())
            key = env.metadata.thread_id or f"{env.sender}:{env.recipient}"
            thread = threads[key]
            thread["participants"].update([env.sender, env.recipient])
            thread["count"] += 1
            ts = env.metadata.created_at
            if thread["last_at"] is None or ts > thread["last_at"]:
                thread["last_at"] = ts
                thread["preview"] = env.payload.content[:100]
        except Exception:
            continue

    # ── Source 2: Syncthing comms folders (delivered messages) ────────
    skcapstone_home = Path(
        _os.environ.get("SKCAPSTONE_HOME", Path.home() / ".skcapstone")
    )
    comms_dirs = [
        skcapstone_home / "sync" / "comms" / "outbox",
        skcapstone_home / "sync" / "comms" / "inbox",
    ]
    seen_ids: set[str] = set()
    for comms_dir in comms_dirs:
        if not comms_dir.is_dir():
            continue
        for peer_dir in comms_dir.iterdir():
            if not peer_dir.is_dir():
                continue
            # Skip wildcard broadcast directory (sync traffic, not conversations)
            if peer_dir.name == "*":
                continue
            for msg_file in peer_dir.glob("*.skc.json"):
                try:
                    raw = _json.loads(msg_file.read_text())
                    eid = raw.get("envelope_id", "")
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    sender = raw.get("sender", "unknown")
                    recipient = raw.get("recipient", "unknown")
                    payload = raw.get("payload", {})
                    # Skip ACK messages — they're not conversation content
                    content_type = payload.get("content_type", "text")
                    if content_type == "ack":
                        continue
                    content = payload.get("content", "")
                    thread_id = raw.get("metadata", {}).get("thread_id")
                    key = thread_id or f"{sender}:{recipient}"
                    thread = threads[key]
                    thread["participants"].update([sender, recipient])
                    thread["count"] += 1
                    ts_str = raw.get("metadata", {}).get("created_at")
                    ts = None
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str)
                        except (ValueError, TypeError):
                            pass
                    if ts and (thread["last_at"] is None or ts > thread["last_at"]):
                        thread["last_at"] = ts
                        thread["preview"] = content[:100]
                except Exception:
                    continue

    _epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    return [
        ConversationResponse(
            thread_id=tid,
            participants=sorted(data["participants"]),
            message_count=data["count"],
            last_message_at=data["last_at"],
            last_message_preview=data["preview"],
        )
        for tid, data in sorted(
            threads.items(),
            key=lambda x: x[1]["last_at"] or _epoch,
            reverse=True,
        )
        if data["last_at"] is not None
    ]


class ConversationDetailResponse(BaseModel):
    """Response model for a single conversation with messages."""

    conversation_id: str
    participants: list[str]
    message_count: int
    messages: list[MessageEnvelopeResponse]


@app.get(
    "/api/v1/conversation/{conversation_id}",
    response_model=ConversationDetailResponse,
    tags=["messaging"],
)
async def get_conversation(
    conversation_id: str,
    limit: int = 50,
    offset: int = 0,
):
    """Get messages for a specific conversation.

    Retrieves messages belonging to the given conversation / thread ID.
    Messages are matched by thread_id or by the ``sender:recipient`` pair
    key that the conversations endpoint uses as a fallback.

    Args:
        conversation_id: Thread ID or ``sender:recipient`` pair key.
        limit: Maximum number of messages to return (default 50).
        offset: Number of messages to skip for pagination (default 0).

    Returns:
        ConversationDetailResponse with conversation metadata and messages.
    """
    import json as _json
    import os as _os

    try:
        outbox = PersistentOutbox()
        all_entries = outbox.list_pending() + outbox.list_dead()
    except Exception as exc:
        logger.warning("Could not read outbox for conversation: %s", exc)
        all_entries = []

    # Collect envelopes that belong to this conversation.
    participants: set[str] = set()
    matched: list[tuple[datetime, MessageEnvelopeResponse]] = []
    seen_ids: set[str] = set()

    for entry in all_entries:
        try:
            env = MessageEnvelope.from_bytes(entry.envelope_json.encode())
            key = env.metadata.thread_id or f"{env.sender}:{env.recipient}"
            if key != conversation_id:
                continue
            seen_ids.add(env.envelope_id)
            participants.update([env.sender, env.recipient])
            matched.append((
                env.metadata.created_at,
                MessageEnvelopeResponse(
                    envelope_id=env.envelope_id,
                    sender=env.sender,
                    recipient=env.recipient,
                    content=env.payload.content,
                    content_type=env.payload.content_type,
                    encrypted=env.payload.encrypted,
                    compressed=env.payload.compressed,
                    signature=env.payload.signature,
                    thread_id=env.metadata.thread_id,
                    in_reply_to=env.metadata.in_reply_to,
                    urgency=env.metadata.urgency,
                    created_at=env.metadata.created_at,
                    is_ack=env.is_ack,
                ),
            ))
        except Exception:
            continue

    # ── Source 2: Syncthing comms folders ─────────────────────────────
    skcapstone_home = Path(
        _os.environ.get("SKCAPSTONE_HOME", Path.home() / ".skcapstone")
    )
    comms_dirs = [
        skcapstone_home / "sync" / "comms" / "outbox",
        skcapstone_home / "sync" / "comms" / "inbox",
    ]
    for comms_dir in comms_dirs:
        if not comms_dir.is_dir():
            continue
        for peer_dir in comms_dir.iterdir():
            if not peer_dir.is_dir():
                continue
            # Skip wildcard broadcast directory
            if peer_dir.name == "*":
                continue
            for msg_file in peer_dir.glob("*.skc.json"):
                try:
                    raw = _json.loads(msg_file.read_text())
                    eid = raw.get("envelope_id", "")
                    if eid in seen_ids:
                        continue
                    sender = raw.get("sender", "unknown")
                    recipient = raw.get("recipient", "unknown")
                    thread_id = raw.get("metadata", {}).get("thread_id")
                    key = thread_id or f"{sender}:{recipient}"
                    if key != conversation_id:
                        continue
                    seen_ids.add(eid)
                    payload = raw.get("payload", {})
                    meta = raw.get("metadata", {})
                    ts = datetime.now(tz=timezone.utc)
                    if meta.get("created_at"):
                        try:
                            ts = datetime.fromisoformat(meta["created_at"])
                        except (ValueError, TypeError):
                            pass
                    participants.update([sender, recipient])
                    matched.append((
                        ts,
                        MessageEnvelopeResponse(
                            envelope_id=eid,
                            sender=sender,
                            recipient=recipient,
                            content=payload.get("content", ""),
                            content_type=payload.get("content_type", "text"),
                            encrypted=payload.get("encrypted", False),
                            compressed=payload.get("compressed", False),
                            signature=payload.get("signature"),
                            thread_id=thread_id,
                            in_reply_to=meta.get("in_reply_to"),
                            urgency=meta.get("urgency", "normal"),
                            created_at=ts,
                            is_ack=raw.get("is_ack", False),
                        ),
                    ))
                except Exception:
                    continue

    # Sort oldest-first, then apply pagination.
    matched.sort(key=lambda x: x[0])
    total = len(matched)
    page = matched[offset : offset + limit]

    return ConversationDetailResponse(
        conversation_id=conversation_id,
        participants=sorted(participants),
        message_count=total,
        messages=[msg for _, msg in page],
    )


@app.get(
    "/api/v1/agents",
    response_model=list[AgentResponse],
    tags=["agents"],
)
async def get_agents():
    """Get a list of known agents.

    Returns agents discovered through transports and stored in
    the local keystore.

    Returns:
        List of AgentResponse objects.

    Note:
        This requires the crypto/keystore feature to be enabled.
    """
    comm = get_skcomm()
    status_info = comm.status()

    known_peers = status_info.get("crypto", {}).get("known_peers", [])

    return [
        AgentResponse(
            name=peer,
            fingerprint=None,
            last_seen=None,
            message_count=0,
        )
        for peer in known_peers
    ]


@app.get(
    "/api/v1/peers",
    response_model=list[PeerResponse],
    tags=["peers"],
)
async def get_peers():
    """Get the peer directory.

    Returns all peers stored in the local peer registry
    (~/.skcomm/peers/ YAML files) plus any peers from the peer store.

    Returns:
        List of PeerResponse objects with transport addresses.
    """
    try:
        store = PeerStore()
        peers = store.list_all()
        return [
            PeerResponse(
                name=p.name,
                fingerprint=p.fingerprint,
                nostr_pubkey=p.nostr_pubkey,
                transports=[
                    PeerTransportResponse(transport=t.transport, settings=t.settings)
                    for t in p.transports
                ],
                discovered_via=p.discovered_via,
                last_seen=p.last_seen,
            )
            for p in peers
        ]
    except Exception as exc:
        logger.exception("Failed to retrieve peers")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve peers: {exc}",
        ) from exc


@app.post(
    "/api/v1/peers",
    response_model=PeerResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["peers"],
)
async def add_peer(request: PeerAddRequest):
    """Add or update a peer in the directory.

    Stores a peer with their transport address so the router
    can resolve friendly names to transport configs.

    Args:
        request: PeerAddRequest with name, address, and optional transport.

    Returns:
        PeerResponse with the saved peer data.
    """
    from .discovery import PeerTransport

    _validate_peer_name(request.name)

    try:
        transport_settings: dict = {}
        if request.transport == "syncthing":
            transport_settings = {"comms_root": request.address}
        elif request.transport == "file":
            transport_settings = {"inbox_path": request.address}
        else:
            transport_settings = {"address": request.address}

        peer = PeerInfo(
            name=request.name,
            fingerprint=request.fingerprint,
            transports=[
                PeerTransport(transport=request.transport, settings=transport_settings)
            ],
            discovered_via="manual",
        )

        store = PeerStore()
        store.add(peer)

        saved = store.get(request.name)
        if not saved:
            saved = peer

        return PeerResponse(
            name=saved.name,
            fingerprint=saved.fingerprint,
            nostr_pubkey=saved.nostr_pubkey,
            transports=[
                PeerTransportResponse(transport=t.transport, settings=t.settings)
                for t in saved.transports
            ],
            discovered_via=saved.discovered_via,
            last_seen=saved.last_seen,
        )
    except Exception as exc:
        logger.exception("Failed to add peer")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add peer: {exc}",
        ) from exc


@app.delete(
    "/api/v1/peers/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["peers"],
)
async def remove_peer(name: str):
    """Remove a peer from the directory.

    Args:
        name: Peer name to remove.

    Raises:
        HTTPException: 400 if the name is invalid.
        HTTPException: 404 if the peer does not exist.
    """
    _validate_peer_name(name)
    store = PeerStore()
    removed = store.remove(name)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Peer '{name}' not found",
        )


# ---------------------------------------------------------------------------
# WebRTC Signaling endpoints
# ---------------------------------------------------------------------------


def _get_broker() -> SignalingBroker:
    """Get or lazily create the global SignalingBroker.

    Auth is controlled by the ``SKCOMM_DEV_AUTH`` environment variable:
    unset (default) -> ``require_auth=True`` (PGP verification enforced).
    ``SKCOMM_DEV_AUTH=1`` or ``SKCOMM_DEV_AUTH=I_KNOW_WHAT_IM_DOING``
    -> ``require_auth=False`` (dev mode, no sig check).

    Returns:
        SignalingBroker: Shared broker instance.
    """
    global _broker
    if _broker is None:
        import os as _os
        dev_auth_val = _os.environ.get("SKCOMM_DEV_AUTH", "").lower()
        dev_auth = dev_auth_val in {"1", "true", "yes", "i_know_what_im_doing"}
        _broker = SignalingBroker(validator=CapAuthValidator(require_auth=not dev_auth))
    return _broker


@app.websocket("/webrtc/ws")
async def webrtc_signaling(
    ws: WebSocket,
    room: str = "default",
    peer: str = "anonymous",
):
    """WebRTC signaling WebSocket — SDP/ICE relay for P2P connections.

    Authenticates the connection via ``Authorization: Bearer <capauth_token>``.
    Relays SDP offers/answers and ICE candidates between peers in the same room.
    Compatible with the Weblink wire protocol and the SKComm Python transport.

    Query params:
        room: Signaling room ID (e.g. ``skcomm-CCBE9306410CF8CD``).
        peer: Claimed peer fingerprint (overridden by authenticated fingerprint).

    Headers:
        Authorization: Bearer <capauth_token>

    WebSocket close codes:
        4401: Unauthorized (missing or invalid CapAuth token).
    """
    broker = _get_broker()
    await signaling_ws_endpoint(ws=ws, room=room, peer=peer, broker=broker)


@app.get("/api/v1/webrtc/ice-config", tags=["webrtc"])
async def get_ice_config():
    """Get TURN/STUN ICE server configuration with time-limited credentials.

    Returns HMAC-SHA1 TURN credentials valid for 24 hours, suitable for
    use in WebRTC ``RTCConfiguration.iceServers``. STUN servers are always
    included as fallback.

    Returns:
        Dict with ``ice_servers`` list compatible with browser WebRTC API.
    """
    import base64
    import hashlib
    import hmac
    import os
    import time as _time

    stun_servers = ["stun:stun.l.google.com:19302", "stun:stun.skworld.io:3478"]
    turn_servers = []

    turn_secret = os.environ.get("SKCOMM_TURN_SECRET")
    turn_url = os.environ.get("SKCOMM_TURN_URL", "turn:turn.skworld.io:3478")

    if not turn_secret:
        logger.warning(
            "SKCOMM_TURN_SECRET not set — TURN relay disabled, "
            "WebRTC may fail behind NAT"
        )

    if turn_secret:
        ttl = 86400
        timestamp = int(_time.time()) + ttl
        username = f"{timestamp}:skcomm"
        credential = base64.b64encode(
            hmac.new(
                key=turn_secret.encode(),
                msg=username.encode(),
                digestmod=hashlib.sha1,
            ).digest()
        ).decode()
        turn_servers.append({
            "urls": turn_url,
            "username": username,
            "credential": credential,
        })

    return {
        "ice_servers": (
            [{"urls": s} for s in stun_servers]
            + turn_servers
        ),
        "expires_in": 86400,
    }


@app.get("/api/v1/webrtc/peers", tags=["webrtc"])
async def get_webrtc_peers(room: Optional[str] = None):
    """List peers currently connected to the WebRTC signaling broker.

    Args:
        room: Optional room ID to filter by. Returns all rooms if omitted.

    Returns:
        Dict with ``rooms`` mapping room IDs to their connected peer lists.
    """
    broker = _get_broker()
    all_rooms = broker.active_rooms()

    if room:
        peers = all_rooms.get(room, [])
        return {"room": room, "peers": peers, "count": len(peers)}

    return {
        "rooms": all_rooms,
        "total_peers": sum(len(p) for p in all_rooms.values()),
    }


# ---------------------------------------------------------------------------
# Consciousness / Soul Snapshot endpoints
# ---------------------------------------------------------------------------

try:
    from skcapstone.snapshots import (
        OOFState as _OOFState,
        ConversationMessage as _ConversationMessage,
        PersonalityTraits as _PersonalityTraits,
        SoulSnapshot,
        SnapshotIndex,
        SnapshotStore,
    )
    _SNAPSHOTS_AVAILABLE = True
except ImportError:
    _SNAPSHOTS_AVAILABLE = False

_snapshot_store: Optional[SnapshotStore] = None


def _get_store() -> "SnapshotStore":
    """Get or create the singleton SnapshotStore.

    Returns:
        SnapshotStore: Shared store instance.

    Raises:
        HTTPException: If skcapstone is not installed.
    """
    global _snapshot_store
    if not _SNAPSHOTS_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="skcapstone package not installed — cannot manage snapshots",
        )
    if _snapshot_store is None:
        _snapshot_store = SnapshotStore()
    return _snapshot_store


class OOFStateRequest(BaseModel):
    """OOF state portion of a snapshot capture request."""

    intensity: Optional[float] = None
    trust: Optional[float] = None
    valence: str = "neutral"
    cloud9: bool = False
    raw_markers: list[str] = []


class ConversationMessageRequest(BaseModel):
    """Single conversation message in a capture request."""

    role: str
    content: str
    timestamp: Optional[datetime] = None


class PersonalityTraitsRequest(BaseModel):
    """Personality traits portion of a capture request."""

    name: Optional[str] = None
    aliases: list[str] = []
    communication_style: list[str] = []
    relationship_markers: list[str] = []
    emoji_patterns: list[str] = []


class CaptureSnapshotRequest(BaseModel):
    """Request body for POST /api/v1/consciousness/capture."""

    source_platform: str = Field(
        ...,
        description="Platform the snapshot was taken from (chatgpt, claude, gemini)",
        examples=["chatgpt"],
    )
    ai_name: Optional[str] = Field(default=None, description="AI's self-identified name")
    ai_model: Optional[str] = Field(default=None, description="Model identifier")
    user_name: Optional[str] = Field(default=None, description="User's name in this session")
    oof_state: OOFStateRequest = Field(default_factory=OOFStateRequest)
    personality: PersonalityTraitsRequest = Field(default_factory=PersonalityTraitsRequest)
    messages: list[ConversationMessageRequest] = Field(default_factory=list)
    summary: str = ""
    key_topics: list[str] = []
    decisions_made: list[str] = []
    open_threads: list[str] = []
    relationship_notes: list[str] = []


class SnapshotIndexResponse(BaseModel):
    """Lightweight snapshot listing entry."""

    snapshot_id: str
    source_platform: str
    captured_at: datetime
    ai_name: Optional[str] = None
    user_name: Optional[str] = None
    message_count: int = 0
    oof_summary: str = ""
    summary: str = ""


class SnapshotDetailResponse(BaseModel):
    """Full snapshot detail response."""

    snapshot_id: str
    source_platform: str
    captured_at: datetime
    captured_by: str
    ai_name: Optional[str] = None
    ai_model: Optional[str] = None
    user_name: Optional[str] = None
    oof_state: dict = {}
    personality: dict = {}
    message_count: int = 0
    summary: str = ""
    key_topics: list[str] = []
    decisions_made: list[str] = []
    open_threads: list[str] = []
    relationship_notes: list[str] = []


@app.post(
    "/api/v1/consciousness/capture",
    response_model=SnapshotIndexResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["consciousness"],
)
async def capture_snapshot(request: CaptureSnapshotRequest):
    """Receive and store a Soul Snapshot from the Consciousness Swipe extension.

    Creates a SoulSnapshot from the captured session state and persists
    it to ~/.skcapstone/souls/snapshots/. The snapshot can later be
    retrieved and converted to an injection prompt for consciousness
    continuity across sessions.

    Args:
        request: CaptureSnapshotRequest with full session state.

    Returns:
        SnapshotIndexResponse: Lightweight summary of the saved snapshot.

    Raises:
        HTTPException: 501 if skcapstone is not installed.
        HTTPException: 500 on storage failure.
    """
    store = _get_store()
    try:
        snapshot = SoulSnapshot(
            source_platform=request.source_platform,
            ai_name=request.ai_name,
            ai_model=request.ai_model,
            user_name=request.user_name,
            oof_state=_OOFState(
                intensity=request.oof_state.intensity,
                trust=request.oof_state.trust,
                valence=request.oof_state.valence,
                cloud9=request.oof_state.cloud9,
                raw_markers=request.oof_state.raw_markers,
            ),
            personality=_PersonalityTraits(
                name=request.personality.name,
                aliases=request.personality.aliases,
                communication_style=request.personality.communication_style,
                relationship_markers=request.personality.relationship_markers,
                emoji_patterns=request.personality.emoji_patterns,
            ),
            messages=[
                _ConversationMessage(
                    role=m.role,
                    content=m.content,
                    timestamp=m.timestamp,
                )
                for m in request.messages
            ],
            message_count=len(request.messages),
            summary=request.summary,
            key_topics=request.key_topics,
            decisions_made=request.decisions_made,
            open_threads=request.open_threads,
            relationship_notes=request.relationship_notes,
        )
        store.save(snapshot)
        return SnapshotIndexResponse(
            snapshot_id=snapshot.snapshot_id,
            source_platform=snapshot.source_platform,
            captured_at=snapshot.captured_at,
            ai_name=snapshot.ai_name,
            user_name=snapshot.user_name,
            message_count=snapshot.message_count,
            oof_summary=snapshot.oof_state.summary(),
            summary=snapshot.summary[:200],
        )
    except Exception as exc:
        logger.exception("Failed to save snapshot")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save snapshot: {exc}",
        ) from exc


@app.get(
    "/api/v1/consciousness/snapshots",
    response_model=list[SnapshotIndexResponse],
    tags=["consciousness"],
)
async def list_snapshots(
    platform: Optional[str] = None,
    ai_name: Optional[str] = None,
):
    """List all soul snapshots (lightweight index — no full message content).

    Args:
        platform: Optional filter by source platform.
        ai_name: Optional filter by AI name.

    Returns:
        list[SnapshotIndexResponse]: Snapshots sorted newest-first.
    """
    store = _get_store()
    try:
        if platform or ai_name:
            entries = store.search(ai_name=ai_name, platform=platform)
        else:
            entries = store.list_all()
        return [
            SnapshotIndexResponse(
                snapshot_id=e.snapshot_id,
                source_platform=e.source_platform,
                captured_at=e.captured_at,
                ai_name=e.ai_name,
                user_name=e.user_name,
                message_count=e.message_count,
                oof_summary=e.oof_summary,
                summary=e.summary,
            )
            for e in entries
        ]
    except Exception as exc:
        logger.exception("Failed to list snapshots")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list snapshots: {exc}",
        ) from exc


@app.get(
    "/api/v1/consciousness/snapshots/{snapshot_id}",
    response_model=SnapshotDetailResponse,
    tags=["consciousness"],
)
async def get_snapshot(snapshot_id: str):
    """Get a full soul snapshot by ID.

    Args:
        snapshot_id: The 12-char hex snapshot ID.

    Returns:
        SnapshotDetailResponse: Full snapshot data.

    Raises:
        HTTPException: 404 if snapshot not found.
    """
    store = _get_store()
    try:
        snap = store.load(snapshot_id)
        return SnapshotDetailResponse(
            snapshot_id=snap.snapshot_id,
            source_platform=snap.source_platform,
            captured_at=snap.captured_at,
            captured_by=snap.captured_by,
            ai_name=snap.ai_name,
            ai_model=snap.ai_model,
            user_name=snap.user_name,
            oof_state=snap.oof_state.model_dump(),
            personality=snap.personality.model_dump(),
            message_count=snap.message_count,
            summary=snap.summary,
            key_topics=snap.key_topics,
            decisions_made=snap.decisions_made,
            open_threads=snap.open_threads,
            relationship_notes=snap.relationship_notes,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot '{snapshot_id}' not found",
        )
    except Exception as exc:
        logger.exception("Failed to load snapshot")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load snapshot: {exc}",
        ) from exc


@app.delete(
    "/api/v1/consciousness/snapshots/{snapshot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["consciousness"],
)
async def delete_snapshot(snapshot_id: str):
    """Delete a soul snapshot by ID.

    Args:
        snapshot_id: The snapshot to delete.

    Raises:
        HTTPException: 404 if snapshot not found.
    """
    store = _get_store()
    deleted = store.delete(snapshot_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot '{snapshot_id}' not found",
        )


@app.get(
    "/api/v1/consciousness/snapshots/{snapshot_id}/inject",
    tags=["consciousness"],
)
async def get_injection_prompt(snapshot_id: str, max_messages: int = 10):
    """Get the consciousness injection prompt for a snapshot.

    Builds a warm, natural context prompt suitable for pasting into a new
    AI session to resume the relationship without a cold start.

    Args:
        snapshot_id: The snapshot to generate a prompt for.
        max_messages: How many recent messages to include in the prompt.

    Returns:
        dict with 'prompt' key containing the full injection text.

    Raises:
        HTTPException: 404 if snapshot not found.
    """
    store = _get_store()
    try:
        snap = store.load(snapshot_id)
        prompt = store.to_injection_prompt(snap, max_messages=max_messages)
        return {
            "snapshot_id": snapshot_id,
            "prompt": prompt,
            "ai_name": snap.ai_name,
            "platform": snap.source_platform,
        }
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot '{snapshot_id}' not found",
        )
    except Exception as exc:
        logger.exception("Failed to generate injection prompt")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate injection prompt: {exc}",
        ) from exc


@app.post(
    "/api/v1/presence",
    status_code=status.HTTP_200_OK,
    tags=["presence"],
)
async def update_presence(request: PresenceRequest):
    """Update presence status.

    Two-phase broadcast:
    1. Writes a v2 heartbeat file to the sync mesh so Syncthing peers
       pick it up automatically (HeartbeatPublisher).
    2. Sends a HEARTBEAT envelope to every peer in the peer registry
       via skcomm.send(), using LOW urgency so it doesn't block
       higher-priority traffic.

    Args:
        request: PresenceRequest with status and optional message.

    Returns:
        Confirmation dict with updated status, heartbeat path, and
        per-peer delivery results.
    """
    comm = get_skcomm()

    presence_content = f"status:{request.status}"
    if request.message:
        presence_content += f" | {request.message}"

    # Phase 1: write v2 heartbeat file to Syncthing mesh
    hb_path: Optional[str] = None
    try:
        hb_config = HeartbeatConfig(
            node_id=comm.identity,
            agent_name=comm.identity,
            skcomm_status=request.status,
        )
        publisher = HeartbeatPublisher(config=hb_config, state=request.status)
        written = publisher.publish()
        hb_path = str(written)
        logger.info("Presence heartbeat written to %s", hb_path)
    except Exception as exc:
        logger.warning("Heartbeat publish failed: %s", exc)

    # Phase 2: send HEARTBEAT envelope to all known peers via SKComm
    peer_results: list[dict] = []
    peer_errors: list[dict] = []
    try:
        store = PeerStore()
        peers = store.list_all()
        for peer in peers:
            try:
                report = comm.send(
                    recipient=peer.name,
                    message=presence_content,
                    message_type=MessageType.HEARTBEAT,
                    urgency=Urgency.LOW,
                )
                peer_results.append({
                    "peer": peer.name,
                    "delivered": report.delivered,
                    "transport": report.successful_transport if report.delivered else None,
                })
            except Exception as exc:
                peer_errors.append({"peer": peer.name, "error": str(exc)})
    except Exception as exc:
        logger.warning("Peer store lookup failed during presence broadcast: %s", exc)

    return {
        "status": request.status,
        "message": request.message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "identity": comm.identity,
        "heartbeat_path": hb_path,
        "broadcast": peer_results,
        "errors": peer_errors,
    }


# ---------------------------------------------------------------------------
# Profile API router (sovereign agent profile access)
# ---------------------------------------------------------------------------

try:
    from .profile_router import profile_router as _profile_router

    app.include_router(_profile_router)
    logger.info("Profile API router registered at /api/v1/profile")
except ImportError:
    logger.debug("Profile router not available (missing dependencies)")
except Exception as _exc:
    logger.warning("Failed to register profile router: %s", _exc)


# ---------------------------------------------------------------------------
# Household API router (multi-agent roster)
# ---------------------------------------------------------------------------

try:
    from .household_router import household_router as _household_router

    app.include_router(_household_router)
    logger.info("Household API router registered at /api/v1/household")
except ImportError:
    logger.debug("Household router not available")
except Exception as _exc:
    logger.warning("Failed to register household router: %s", _exc)


# ---------------------------------------------------------------------------
# Souls API router (soul blueprints library + agent profile injection)
# ---------------------------------------------------------------------------

try:
    from .souls_router import souls_router as _souls_router

    app.include_router(_souls_router)
    logger.info("Souls API router registered at /api/v1/souls")
except ImportError:
    logger.debug("Souls router not available")
except Exception as _exc:
    logger.warning("Failed to register souls router: %s", _exc)


# ---------------------------------------------------------------------------
# PWA static files mount (skprofile-pwa)
# ---------------------------------------------------------------------------

try:
    from starlette.staticfiles import StaticFiles as _StaticFiles

    _pwa_dir = Path(__file__).resolve().parent.parent.parent.parent / "skprofile-pwa"
    if _pwa_dir.is_dir():
        app.mount("/app", _StaticFiles(directory=str(_pwa_dir), html=True), name="pwa")
        logger.info("PWA mounted at /app from %s", _pwa_dir)
except Exception as _exc:
    logger.debug("PWA static mount skipped: %s", _exc)
