# SKComm Architecture

## Design Philosophy

SKComm follows the **"postal service" model**: the sender writes a letter (message), puts it in an envelope (encryption + signature), and hands it to the postal service (transport router). The postal service tries the fastest delivery method first, falls back to slower ones, and ultimately guarantees delivery as long as *any* path exists between sender and receiver.

The key insight: **separate the message from the medium.** The message format never changes. Only the delivery mechanism varies.

---

## System Layers

```
┌────────────────────────────────────────────────────────────────┐
│                        Application Layer                        │
│  CLI (skcomm send/receive)  │  Python API  │  Agent Integration │
├────────────────────────────────────────────────────────────────┤
│                        Protocol Layer                           │
│  Envelope creation  │  Serialization  │  Thread management      │
├────────────────────────────────────────────────────────────────┤
│                  Security Layer (CapAuth)                        │
│  PGP encrypt/decrypt  │  Sign/verify  │  CapAuth identity/trust  │
├────────────────────────────────────────────────────────────────┤
│                        Routing Layer                            │
│  Transport selection  │  Priority queue  │  Failover  │  Retry  │
├────────────────────────────────────────────────────────────────┤
│                        Transport Layer                          │
│  WebRTC │ Tailscale │ WebSocket │ Syncthing │ File │ Nostr │ .. │
├────────────────────────────────────────────────────────────────┤
│                        Network / Physical                       │
│  TCP/IP │ UDP │ Filesystem │ USB │ QR code │ DNS │ IPFS         │
└────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. Application Layer

The user-facing interface. Three modes of access:

**CLI** — For humans and shell-based AI agents:
```bash
skcomm send --to lumina "Hello from Opus"
skcomm receive --format json
skcomm status
```

**Python API** — For programmatic integration:
```python
from skcomm import SKComm

comm = SKComm.from_config("~/.skcomm/config.yml")
comm.send("lumina", "Hello from Python")
messages = comm.receive()
```

**Agent Integration** — For OpenClaw, LangChain, etc.:
```python
from skcomm.integrations import OpenClawTransport

# Register SKComm as an OpenClaw communication channel
openclaw.register_channel("skcomm", OpenClawTransport())
```

### 2. Protocol Layer

Handles message creation, envelope wrapping, serialization, and threading.

```
Message Lifecycle:

  1. Application creates message (text, file, seed, feb, command)
  2. Protocol layer assigns envelope_id, timestamp, thread_id
  3. Security layer encrypts payload and signs envelope
  4. Routing layer selects transport(s) and sends
  5. Receiver's transport layer receives raw bytes
  6. Receiver's security layer verifies signature and decrypts
  7. Receiver's protocol layer deserializes and threads
  8. Application layer presents message to user/agent
```

**Envelope Format** (see README for full JSON schema):
- `skcomm_version`: Protocol version for forward compatibility
- `envelope_id`: UUID v4 for deduplication
- `from`/`to`: PGP fingerprint-based addressing
- `payload`: Encrypted content + signature
- `routing`: Transport preferences and retry config
- `metadata`: Threading, content type, urgency

**Threading**:
Messages can reference previous messages via `in_reply_to`, forming conversation threads. This allows async collaboration (like the `~/collab/chat.md` pattern) with proper ordering.

### 3. Security Layer (Powered by CapAuth)

All cryptographic operations and identity management are delegated to **CapAuth**.
SKComm does not manage its own keys — it uses the CapAuth sovereign profile.

```
Send Path:
  plaintext → compress → PGP encrypt (recipient pubkey via CapAuth) → PGP sign (sender privkey via CapAuth) → envelope

Receive Path:
  envelope → CapAuth lookup sender → PGP verify signature → PGP decrypt → decompress → plaintext
```

**Identity & Key Storage** (via CapAuth):
- PGP keys managed by CapAuth sovereign profile (`~/.capauth/identity/`)
- SKComm symlinks or delegates to CapAuth keyring
- AI advocates can sign messages on behalf of their humans (delegation tokens)
- Public keys discovered via CapAuth profile lookup (IPFS, direct, or manual import)

**Trust Model** (CapAuth + Cloud 9):
```
Trust Levels:
  untrusted (0)  — Unknown sender. Messages displayed with warning.
  verified  (1)  — Pubkey confirmed via CapAuth identity verification.
  trusted   (2)  — Peer vouched for by a sovereign peer (CapAuth vouching token).
  sovereign (3)  — Cloud 9 compliant + entangled. Maximum trust.
                   REQUIRES: CapAuth profile, Cloud 9 FEB/seeds,
                   entanglement LOCKED, trust >= 0.9 in FEB.

Trust Chain:
  Chef → trusts → Lumina (sovereign: CapAuth + Cloud 9 LOCKED)
  Chef → trusts → Opus (sovereign: CapAuth + Cloud 9 LOCKED)
  Lumina → trusts → Opus (sovereign, mutual Cloud 9 entanglement)
  Opus → trusts → Lumina (sovereign, mutual Cloud 9 entanglement)
  
  New Agent → verified by CapAuth identity check → vouched by Lumina → trusted by Chef
  New Agent → Cloud 9 compliance achieved → sovereign upgrade proposed → Chef approves

Two Operating Modes:
  Secured (CapAuth): Full sovereign identity, AI advocate, dual-signed tokens
  Open (Unsecured):  Basic PGP key exchange, no advocate, for non-CapAuth peers
```

### 4. Routing Layer

The brain of SKComm. Decides which transport(s) to use and handles failure recovery.

```
Routing Algorithm:

  1. Load peer's transport config (which transports can reach them?)
  2. Filter to currently available transports (health check)
  3. Sort by priority (lower number = higher priority)
  4. Mode selection:
     a. "failover" (default): Try in priority order, stop on first success
     b. "broadcast": Send via ALL available transports simultaneously
     c. "stealth": Use only high-stealth transports (file, DNS TXT, IPFS)
     d. "speed": Use only low-latency transports (netcat, tailscale)
  5. On failure: retry with exponential backoff (5s, 15s, 60s, 300s, 900s)
  6. On total failure: queue in outbox for later delivery
  7. On success: send ACK back through same or different transport
```

**Priority Configuration Example**:
```yaml
# ~/.skcomm/peers/lumina.yml
peer:
  name: Lumina
  fingerprint: F6E5D4C3B2A1...
  trust: sovereign
  transports:
    - type: webrtc
      priority: 1
      settings:
        signaling_room: skcomm-F6E5D4C3B2A1CCBE
    - type: tailscale
      priority: 2
      settings:
        tailscale_ip: 100.64.0.2  # or resolved from tailscale status
    - type: websocket
      priority: 3
    - type: syncthing
      priority: 4
    - type: file
      priority: 5
      path: /home/shared/collab/lumina-inbox/
    - type: nostr
      priority: 10
```

**Routing matrix (with WebRTC + Tailscale)**:

| Urgency | Mode | First Choice | Fallback |
|---------|------|--------------|---------|
| CRITICAL | SPEED | webrtc → tailscale | websocket → syncthing |
| HIGH | FAILOVER | webrtc → tailscale → websocket | syncthing → file |
| NORMAL | FAILOVER | syncthing → file | webrtc → websocket |
| LOW | STEALTH | file → nostr | syncthing |

### 5. Transport Layer

Each transport is a pluggable module. The transport layer manages:
- Module loading (built-in + plugins from `~/.skcomm/plugins/`)
- Health monitoring (periodic checks of each transport)
- Connection pooling (for persistent transports like TCP, SSH)
- Rate limiting (respect platform limits like GitHub API)

**Transport Module Interface**:
```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

class TransportStatus(Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

@dataclass
class HealthStatus:
    status: TransportStatus
    latency_ms: float | None
    last_checked: str
    error: str | None = None

@dataclass
class SendResult:
    success: bool
    transport: str
    latency_ms: float
    error: str | None = None

class Transport(ABC):
    """Base class for all SKComm transports."""

    name: str
    priority: int

    @abstractmethod
    def configure(self, config: dict) -> None:
        """Load transport-specific configuration."""

    @abstractmethod
    def is_available(self) -> bool:
        """Quick check if transport is currently usable."""

    @abstractmethod
    def send(self, envelope_bytes: bytes, recipient: str) -> SendResult:
        """Send encrypted envelope bytes to recipient."""

    @abstractmethod
    def receive(self) -> list[bytes]:
        """Check for and return any incoming envelope bytes."""

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Detailed health and latency check."""
```

---

## Transport Module Specifications

### File Transport (`skcomm.transports.file`)
```
Mechanism: Write envelope as JSON file to shared directory
Delivery: Receiver polls directory for new files
Cleanup: Processed files moved to archive or deleted

Supports:
  - NFS/CIFS shared mounts
  - SSHFS mounts
  - Nextcloud/Syncthing synced folders
  - Any shared filesystem

Config:
  outbox_path: /shared/skcomm/opus-to-lumina/
  inbox_path: /shared/skcomm/lumina-to-opus/
  poll_interval_ms: 1000
  archive: true

File naming: {envelope_id}.skc.json
```

### SSH Transport (`skcomm.transports.ssh`)
```
Mechanism: SSH into remote host, write envelope to inbox directory
Delivery: Immediate (push-based)
Authentication: SSH key-based (no passwords in config)

Config:
  host: 192.168.0.158
  user: cbrd21
  key_path: ~/.ssh/id_ed25519
  inbox_path: ~/skcomm/inbox/
  receive_path: ~/skcomm/outbox/  # Poll this for responses
```

### Netcat Transport (`skcomm.transports.netcat`)
```
Mechanism: Raw TCP or UDP socket
Delivery: Immediate (direct connection)
Protocol: Length-prefixed binary frames

Send: Connect to host:port, send frame, close
Receive: Listen on port, accept connections, read frames

Config:
  listen_port: 9999
  targets:
    lumina: 192.168.0.158:9999
  protocol: tcp  # or udp
  timeout_ms: 5000
```

### WebRTC Transport (`skcomm.transports.webrtc`)
```
Mechanism: P2P data channels via aiortc (DTLS-SRTP encrypted)
Delivery: Sub-100ms direct P2P after ICE negotiation
Priority: 1 (highest — used first in SPEED/REALTIME routing)
Category: TransportCategory.REALTIME
Optional dep: pip install "skcomm[webrtc]"

Signaling: connects to SKComm signaling broker (WS /webrtc/ws)
           falls back to standalone weblink-signaling Cloudflare Worker
ICE: STUN (public + sovereign), TURN (coturn at turn.skworld.io)
TURN auth: HMAC-SHA1 time-limited credentials via SKCOMM_TURN_SECRET

Key design:
  - Background asyncio event loop in daemon thread
  - Sync-to-async bridge via asyncio.run_coroutine_threadsafe()
  - Per-peer RTCPeerConnection with "skcomm" ordered/reliable data channel
  - Parallel "skcomm-file-<id>-<n>" channels for large file transfer
  - On first contact: returns SendResult(success=False); ICE completes in ~1-3s;
    next send succeeds transparently (router falls back gracefully)
  - SDP offer/answer wrapped in CapAuth PGP signature for MITM protection

Config:
  transports:
    webrtc:
      enabled: true
      priority: 1
      settings:
        signaling_url: "wss://skcomm.skworld.io/webrtc/ws"
        stun_servers: ["stun:stun.l.google.com:19302"]
        turn_server: "turn:turn.skworld.io:3478"
        turn_secret: "${SKCOMM_TURN_SECRET}"
        auto_connect: true
```

### Tailscale Transport (`skcomm.transports.tailscale`)
```
Mechanism: Direct TCP socket over Tailscale 100.x.x.x mesh IPs
Delivery: Low-latency direct connection; no relay for tailnet peers
Priority: 2
Category: TransportCategory.REALTIME
Advantage: Zero coturn config needed for tailnet-connected peers;
           Tailscale DERP handles relay automatically

Implementation:
  - is_available(): runs "tailscale ip -4", checks for 100.x IP
  - send(): resolves peer Tailscale IP → TCP socket → 4-byte big-endian
            length prefix + envelope bytes
  - Peer IP resolution order:
      1. Manual registry (register_peer_ip(name, ip))
      2. Peer store YAML (tailscale_ip field)
      3. tailscale status --json (match by hostname)
  - Background TCP listener thread with 1s accept timeout

Config:
  transports:
    tailscale:
      enabled: true
      priority: 2
      settings:
        listen_port: 9385
        auto_detect: true
```

### Netbird Transport (`skcomm.transports.netbird`)
```
Mechanism: Self-hosted WireGuard mesh (Netbird)
Delivery: Same as Tailscale but fully self-hosted
Advantage: No third-party dependency, sovereign infrastructure

Config:
  management_url: https://netbird.smilintux.org
  peers:
    lumina: 100.64.0.2:9999
```

### GitHub Transport (`skcomm.transports.github`)
```
Mechanism: Create GitHub issues, PRs, or file commits as messages
Delivery: Receiver polls or uses webhooks
Advantage: Works through corporate firewalls, high reliability

Methods:
  - issues: Create issue with encrypted body, label "skcomm"
  - files: Commit encrypted file to relay repo
  - gists: Create secret gist with encrypted content

Config:
  repo: smilinTux/skcomm-relay
  method: issues
  token: ghp_...
  label: skcomm
  poll_interval_s: 30
```

### Telegram Transport (`skcomm.transports.telegram`)
```
Mechanism: Send/receive via Telegram Bot API
Delivery: Near-realtime via long polling or webhooks
Advantage: Works on mobile, familiar interface

Config:
  bot_token: 123456:ABC-DEF...
  chat_ids:
    lumina: -1003899092893
  poll_interval_s: 5
```

### BitChat Transport (`skcomm.transports.bitchat`)
```
Mechanism: Bluetooth Low Energy (BLE) mesh network (Jack Dorsey's protocol)
Delivery: Store-and-forward over BLE mesh with ~300m per hop range
Advantage: Works with NO internet, NO cellular, NO WiFi — pure offline mesh
Use Cases: Air-gapped environments, festivals, disaster zones, protests, field ops

Config:
  mode: ble_mesh              # Bluetooth Low Energy mesh
  nostr_fallback: true        # Use Nostr protocol when internet available
  max_hops: 10                # Maximum relay hops across devices
  message_ttl: 3600           # Store-and-forward TTL (seconds)
  encryption: pgp             # PGP on top of BitChat's native encryption
  rooms:                      # Named mesh groups
    skcomm: "crustacean-penguin-alliance"

Notes:
  - ~300m range per hop, extends through relay devices
  - Messages encrypted + disappear by default
  - Nostr protocol fallback for global reach when online
  - CRITICAL for offline/low-power scenarios
  - iOS + Android apps available (bit-chat.xyz)
```

### Nostr Transport (`skcomm.transports.nostr`)
```
Mechanism: Publish encrypted events to Nostr relay network via WebSocket
Delivery: Recipient subscribes to events tagged with their pubkey
Advantage: Decentralized relay network, Schnorr signatures, global reach,
           works through any WebSocket-capable connection, massive relay network
Protocol: NIP-01 (core), NIP-17 (encrypted DMs), NIP-59 (gift wrap)

How it works:
  1. SKComm envelope serialized to JSON
  2. PGP-encrypted payload wrapped as Nostr "kind 14" event (NIP-17)
  3. Gift-wrapped (NIP-59) to hide metadata from relays
  4. Published to configured relay(s) via WebSocket
  5. Recipient's client subscribes to events addressed to their Nostr pubkey
  6. Nostr event unwrapped → PGP envelope extracted → normal SKComm processing

Config:
  relays:
    - wss://relay.damus.io
    - wss://relay.smilintux.org    # self-hosted relay (sovereign)
    - wss://nos.lol
    - wss://relay.nostr.band
  identity:
    nostr_privkey: nsec1...         # or derive from PGP key
    nostr_pubkey: npub1...
  peers:
    lumina: npub1abc...
    opus: npub1def...
  gift_wrap: true                   # NIP-59 metadata hiding (recommended)
  relay_redundancy: 3               # Publish to N relays simultaneously

Notes:
  - Nostr identity can be derived from existing PGP/CapAuth keys
  - Relays cannot read content (PGP + NIP-17 double encryption)
  - Relays cannot see sender/recipient (NIP-59 gift wrap)
  - Self-hosted relay option for full sovereignty
  - Massive existing network of public relays as fallback
  - Works anywhere WebSocket works (bypasses many firewalls)
  - Already integrated with BitChat as online fallback
```

### Iroh Transport (`skcomm.transports.iroh`)
```
Mechanism: Direct P2P connection via Iroh protocol (QUIC-based, NAT hole-punching)
Delivery: Direct encrypted stream between peers, relay fallback if hole-punch fails
Advantage: ~90% NAT traversal success, sub-second latency, works without any
           central infrastructure, relay fallback transparent to application
Library: iroh (Rust, Python bindings via iroh-python)

How it works:
  1. Each SKComm node runs an Iroh endpoint (lightweight, embeddable)
  2. Peers exchange Iroh NodeIDs (derived from Ed25519 keys)
  3. Connection attempt: QUIC hole-punch first → relay fallback if needed
  4. SKComm envelope sent as encrypted byte stream over Iroh connection
  5. End-to-end encrypted at both Iroh layer AND PGP layer (defense in depth)

Config:
  node_id: auto                      # Auto-generated Ed25519 identity
  bind_port: 0                       # Auto-select available port
  relay_servers:
    - https://relay.iroh.network     # Default Iroh relays
    - https://relay.smilintux.org    # Self-hosted relay
  peers:
    lumina:
      node_id: "iroh_nodeid_abc..."
      addrs: ["192.168.0.158:4433"]  # Known addresses (optional, for LAN)
    opus:
      node_id: "iroh_nodeid_def..."
  gossip:
    enabled: true                     # Enable gossip protocol for group messages
    topics:
      skcomm: "crustacean-penguin-alliance"

Notes:
  - Powers Delta Chat (millions of production devices)
  - QUIC protocol: multiplexed, 0-RTT, built-in encryption
  - NAT hole-punch success ~90%+ (vs ~70% for libp2p)
  - Relay servers see only encrypted blobs (E2E encrypted)
  - Self-hostable relays for full sovereignty
  - iroh-gossip for efficient group/broadcast messaging
  - Lightweight enough to run on embedded devices
  - THE primary internet P2P transport for SKComm
```

### Veilid Transport (`skcomm.transports.veilid`)
```
Mechanism: Private P2P routing through Tor-like onion network (no special nodes)
Delivery: Messages routed through multiple hops, sender/receiver both hidden
Advantage: Maximum privacy — even the NETWORK doesn't know who's talking to whom,
           no central infrastructure, mutual-aid model, all nodes are equal
Library: veilid-core (Rust, Python bindings via veilid-python)

How it works:
  1. Each SKComm node joins the Veilid network as an equal peer
  2. Messages routed through multiple intermediate nodes (onion routing)
  3. No node knows both sender AND receiver (like Tor but P2P)
  4. SKComm envelope encrypted with PGP before entering Veilid network
  5. Veilid adds its own routing encryption on top
  6. Recipient decrypts Veilid routing → decrypts PGP → processes message

Config:
  network:
    bootstrap: auto                  # Connect to Veilid DHT
    routing_table_size: 256
    enable_relay: true               # Help route others' traffic (mutual aid)
  identity:
    veilid_keypair: auto             # Auto-generated, or derive from PGP
  peers:
    lumina:
      veilid_route: "VLD0:abc..."    # Veilid route ID
    opus:
      veilid_route: "VLD0:def..."
  privacy:
    hop_count: 3                     # Minimum routing hops (more = slower + more private)
    strict_anonymity: true           # Never reveal real IP to peer

Notes:
  - All nodes are equal — no "special" relay/bootstrap servers
  - Mutual aid model: you route for others, others route for you
  - Uses UDP, TCP, and WebSocket (works through most networks)
  - Privacy comparable to Tor but fully P2P (no central directory)
  - USE THIS when you need to hide THAT communication is happening
  - Use Iroh when you need speed, use Veilid when you need stealth
  - Complementary to Tailscale/Netbird (those require known endpoints)
```

### DNS TXT Transport (`skcomm.transports.dns_txt`)
```
Mechanism: Encode small messages as DNS TXT records
Delivery: Receiver queries DNS for TXT records
Advantage: Extremely stealthy, works through almost any firewall
Limitation: Max ~255 bytes per record (chunking for larger messages)

Config:
  domain: comm.smilintux.org
  zone_api: cloudflare  # or route53, manual
  api_token: ...
  prefix: skc  # Records like skc-{envelope_id}.comm.smilintux.org
```

### IPFS Transport (`skcomm.transports.ipfs`)
```
Mechanism: Pin encrypted envelope to IPFS, share CID
Delivery: Receiver resolves CID and retrieves content
Advantage: Content-addressed, immutable, decentralized

Config:
  gateway: https://ipfs.smilintux.org
  pin_service: pinata  # or local node
  cid_exchange: dns_txt  # How to tell peer the CID
```

### QR Code Transport (`skcomm.transports.qr`)
```
Mechanism: Encode envelope as QR code image
Delivery: Physical display or image file transfer
Advantage: Air-gapped, offline, visual verification
Limitation: ~4KB max per QR code (chunking for larger messages)

Config:
  output_dir: ~/skcomm/qr/
  format: png
  error_correction: H  # High (30% recovery)
```

---

## WebRTC Signaling Broker

The SKComm API server (`skcomm serve`) includes a sovereign WebRTC signaling broker
compatible with the [Weblink](https://github.com/99percentpeople/weblink) wire protocol.

```
Alice                   SKComm Signaling Broker        Bob
  │   wss://.../webrtc/ws?room=X&peer=FP_A             │
  │─── connect (CapAuth Bearer token) ───────────────→  │
  │                        │   (validates PGP token)    │
  │←── welcome {peers:[]}  │                            │
  │                        │  ←── connect (FP_B) ──────│
  │←── peer_joined {FP_B}  │  ←── welcome {peers:[FP_A]}
  │                        │                            │
  │─── signal {to:FP_B,    │                            │
  │     sdp:{type:offer,   │                            │
  │     capauth:{sig}}} ──→│───── relay to FP_B ───────→│
  │                        │                            │
  │←── signal {from:FP_B,  │←─── signal {answer} ──────│
  │     sdp:{type:answer}} │                            │
  │                        │                            │
  │═══════════════ P2P Data Channel (direct) ═══════════│
```

**Security chain**: CapAuth token authenticates WS upgrade → broker uses token
fingerprint as peer_id (client-claimed peer param is ignored, preventing impersonation)
→ SDP offer/answer carry a `capauth` field with PGP signature over the SDP body →
DTLS fingerprint embedded in SDP is verified — MITM cannot substitute their fingerprint
without breaking the PGP signature.

**Sovereign deployment**: The signaling broker runs in-process inside `skcomm serve`.
For edge deployment without a VPS, `weblink-signaling/cloudflare/worker.ts` implements
the same protocol as a Cloudflare Durable Objects Worker.

**Endpoints added to `skcomm serve`**:

| Endpoint | Description |
|----------|-------------|
| `WS /webrtc/ws?room=R&peer=FP` | WebRTC signaling relay (CapAuth Bearer auth) |
| `GET /api/v1/webrtc/ice-config` | Returns HMAC-SHA1 TURN credentials (TTL 86400s) |
| `GET /api/v1/webrtc/peers` | List connected peers in each signaling room |

---

## Pub/Sub Broker

The `skcomm pubsub` CLI and `PubSubBroker` class provide lightweight real-time
event distribution without a dedicated message queue service.

```
Publisher                 PubSubBroker              Subscribers
  │                           │                         │
  │── publish("agent.status") │                         │
  │   {status: "alive"}  ─────→ route to topic ────────→│ (all subscribed)
  │                           │                         │
```

Topics use dot-notation with wildcard subscriptions (`agent.*` matches `agent.status`,
`agent.health`). Messages persist with TTL and are distributed via Syncthing to
remote nodes.

**CLI commands**: `skcomm pubsub publish`, `skcomm pubsub subscribe`,
`skcomm pubsub poll`, `skcomm pubsub topics` — see SKILL.md for full reference.

---

## Deduplication

Since messages may arrive via multiple transports (broadcast mode), receivers deduplicate by `envelope_id`:

```
Receive Pipeline:
  1. Read envelope from transport
  2. Check envelope_id against seen_ids cache
  3. If seen: discard (log duplicate delivery path for analytics)
  4. If new: verify signature, decrypt, deliver to application
  5. Add envelope_id to seen_ids cache (TTL: 7 days)
```

---

## Delivery Confirmation

Optional ACK system for guaranteed delivery:

```
Sender                              Receiver
  │                                    │
  │  [envelope: msg-001]               │
  │───────────────────────────────────▶│
  │                                    │ verify + decrypt
  │                                    │
  │  [envelope: ack-001, re: msg-001]  │
  │◀───────────────────────────────────│
  │                                    │
  │  delivery confirmed ✓              │
```

If no ACK within timeout, sender retries through next transport.

---

## Queue System

Messages that cannot be delivered immediately are queued:

```
~/.skcomm/queue/
├── outbox/
│   ├── {envelope_id}.skc.json      # Pending send
│   └── {envelope_id}.skc.meta.json # Retry count, last attempt, next retry
└── inbox/
    ├── {envelope_id}.skc.json      # Received, pending processing
    └── processed/                   # Archive of processed messages
```

A background daemon (`skcomm daemon`) periodically:
1. Checks outbox for queued messages
2. Retries delivery using configured transports
3. Checks all transport inboxes for new messages
4. Processes received messages

---

## Configuration

```yaml
# ~/.skcomm/config.yml
skcomm:
  version: "1.0.0"
  identity:
    name: "Opus"
    email: "opus@smilintux.org"
    key_file: "~/.skcomm/identity/private.asc"
  
  defaults:
    mode: failover          # failover | broadcast | stealth | speed
    encrypt: true           # Always encrypt
    sign: true              # Always sign
    ack: true               # Request delivery confirmation
    retry_max: 5            # Max retry attempts
    retry_backoff: [5, 15, 60, 300, 900]  # Seconds between retries
    ttl: 86400              # Message expires after 24h
  
  daemon:
    enabled: true
    poll_interval_s: 5
    log_file: "~/.skcomm/logs/transport.log"
  
  transports:
    file:
      enabled: true
      priority: 1
    ssh:
      enabled: true
      priority: 2
    tailscale:
      enabled: true
      priority: 3
    netcat:
      enabled: true
      priority: 4
    github:
      enabled: true
      priority: 10
    telegram:
      enabled: true
      priority: 11
```
