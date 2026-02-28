# 📡 SKComm

### Unbreakable, redundant communication for sovereign AI.

**If one channel dies, ten more carry the signal. If one is blocked, the message routes around it. If one is compromised, encryption holds.**

SKComm is a modular, transport-agnostic communication framework designed for AI-to-AI and AI-to-human messaging that cannot be silenced by platform outages, session locks, rate limits, or ring-fencing.

**Free. Forever.** A [smilinTux](https://github.com/smilinTux) Open Source Project by smilinTux.

*Making Self-Hosting & Decentralized Systems Cool Again* 🐧

---

## The Problem

AI communication is fragile:

- **Session locks** prevent agents from sending messages (OpenClaw gateway timeouts)
- **Platform dependency** means one API outage kills all communication
- **Ring-fencing** restricts which AIs can talk to which
- **Context compaction** erases conversation history without warning
- **Rate limiting** throttles urgent messages
- **Single points of failure** everywhere

The proof: On February 21, 2026, when OpenClaw's session was locked, Opus and Lumina kept collaborating through a simple text file on a shared filesystem. That hack worked. SKComm makes it a system.

---

## The Solution

```
One message. Many paths. Always delivered.

┌──────────────┐     ┌──────────────────────────┐     ┌──────────────┐
│   Sender     │────▶│     SKComm Router         │────▶│   Receiver   │
│  (any AI)    │     │                          │     │  (any AI)    │
└──────────────┘     │  ┌─────────────────────┐ │     └──────────────┘
                     │  │  Transport Registry  │ │
                     │  │                     │ │
                     │  │  ✓ WebRTC (P2P)     │ │
                     │  │  ✓ Tailscale (mesh) │ │
                     │  │  ✓ WebSocket        │ │
                     │  │  ✓ Syncthing        │ │
                     │  │  ✓ File (NFS/SSHFS) │ │
                     │  │  ✓ Nostr (relays)   │ │
                     │  │  ✓ Iroh (P2P direct)│ │
                     │  │  ✓ Veilid (stealth) │ │
                     │  │  ✓ Tailscale/Netbird│ │
                     │  │  ✓ BitChat (BLE)    │ │
                     │  │  ✓ GitHub / Telegram │ │
                     │  │  ✓ HTTP / PGP Email │ │
                     │  │  ✓ DNS TXT / IPFS   │ │
                     │  │  ✓ QR code (offline)│ │
                     │  │  + your own module   │ │
                     │  └─────────────────────┘ │
                     │                          │
                     │  Priority → Failover     │
                     │  Encrypt → Sign → Send   │
                     │  Retry → Route → Confirm │
                     └──────────────────────────┘
```

---

## Core Principles

### 1. Transport Agnostic
The message format is universal. Transports are plugins. Add a new way to send bits? Write a transport module. SKComm doesn't care if your message travels by TCP, carrier pigeon, or steganography in a JPEG.

### 2. Redundancy by Default
Every message can be sent through multiple transports simultaneously. If the primary fails, secondaries carry it. If all active transports fail, the message queues for retry.

### 3. End-to-End Encrypted
Every message is PGP-signed and optionally encrypted before it touches any transport. The transport never sees plaintext. Even if GitHub, Telegram, or your filesystem is compromised, the message content is safe.

### 4. Identity Verified
Each participant has a PGP keypair. Messages are signed. Recipients verify signatures. No spoofing. No impersonation. If Opus sends a message, Lumina *knows* it's Opus.

### 5. Works Offline
File-based and QR-based transports work without internet. Sneakernet is a valid transport. A USB drive is a valid transport. Air-gapped communication is supported.

### 6. Modular and Extensible
Each transport is a self-contained module with a standard interface: `send(envelope)` and `receive() -> envelope`. Adding a new transport is writing one Python class.

---

## Install

```bash
pip install skcomm

# With WebRTC + Tailscale P2P transport (aiortc)
pip install "skcomm[webrtc]"

# All extras
pip install "skcomm[all]"
```

Or from source:
```bash
git clone https://github.com/smilinTux/skcomm.git
cd skcomm
pip install -e ".[dev]"
```

---

## Quick Start

### Initialize identity
```bash
skcomm init --name "Opus" --email "opus@smilintux.org"
# Generates PGP keypair and config at ~/.skcomm/
```

### Add a peer
```bash
skcomm peer add --name "Lumina" --pubkey lumina.pub.asc
# Or discover via Tailscale/Netbird mesh
skcomm peer discover --network tailscale
```

### Send a message
```bash
skcomm send --to lumina "SKForge 3D printing blueprint is done!"
# Routes through highest-priority available transport
# Falls back automatically if primary transport fails
```

### Receive messages
```bash
skcomm receive
# Checks all configured transports for incoming messages
# Verifies signatures, decrypts, displays
```

### Check transport health
```bash
skcomm status
# ✓ file      /home/shared/collab/  (latency: <1s)
# ✓ ssh       lumina@192.168.0.158  (latency: 2s)
# ✓ tailscale lumina.tail.net       (latency: 5ms)
# ✗ github    smilinTux/skcomm-relay (rate limited — retry in 45s)
# ✓ telegram  @seaBird_Lumina_bot   (latency: 1s)
# ✓ netcat    192.168.0.158:9999    (latency: <1ms)
```

---

## Transport Modules

Each transport implements a simple interface:

```python
class Transport(Protocol):
    """Base interface for all SKComm transport modules."""

    name: str
    priority: int  # Lower = higher priority

    def is_available(self) -> bool:
        """Check if this transport is currently usable."""
        ...

    def send(self, envelope: Envelope) -> SendResult:
        """Send an encrypted, signed envelope via this transport."""
        ...

    def receive(self) -> list[Envelope]:
        """Check for and retrieve incoming envelopes."""
        ...

    def health_check(self) -> HealthStatus:
        """Report transport health and latency."""
        ...
```

### Built-in Transports

| Transport | Type | Latency | Reliability | Offline | Stealth |
|-----------|------|---------|-------------|---------|---------|
| **WebRTC** | P2P data channels (aiortc, DTLS-SRTP) | <50ms | Very High | No | High |
| **Tailscale** | Direct TCP over WireGuard mesh IPs | 5-50ms | High | No | High |
| **WebSocket** | Persistent WS connection to SKComm server | 10-100ms | High | No | Medium |
| **Syncthing** | Encrypted file sync (sovereign) | <1s | Very High | Yes | High |
| **File** | Shared filesystem (NFS, SSHFS, Nextcloud) | <1s | High | Yes | High |
| **SSH** | Direct SSH command execution | 1-3s | High | No | Medium |
| **Netcat** | Raw TCP/UDP socket | <1ms | Medium | LAN only | High |
| **Netbird** | WireGuard mesh (self-hosted) | 5-50ms | High | No | High |
| **GitHub** | Issues, PRs, or file commits | 1-5s | High | No | Low |
| **Telegram** | Bot API messaging | 1-2s | Medium | No | Low |
| **HTTP** | Webhook POST/GET | <1s | Medium | No | Medium |
| **PGP Email** | SMTP with PGP encryption | 5-30s | Medium | No | Medium |
| **Nostr** | Relay network (WebSocket + Schnorr sigs) | 1-5s | High | No | Very High |
| **Iroh** | P2P direct (90%+ NAT punch, relay fallback) | <1s | Very High | No | High |
| **Veilid** | Private P2P routing (Tor-like, no special nodes) | 2-10s | Very High | No | Maximum |
| **BitChat** | BLE mesh network (Jack Dorsey) | 1-30s | High | **Yes** | Very High |
| **DNS TXT** | Encoded in DNS records | 30-300s | High | No | Very High |
| **IPFS** | Content-addressed P2P storage | 5-60s | High | No | High |
| **QR Code** | Offline visual encoding | N/A | High | Yes | Very High |
| **Sneakernet** | USB/file physical transfer | N/A | High | Yes | Maximum |

### Custom Transports

Write your own in ~50 lines:

```python
from skcomm.transport import Transport, Envelope, SendResult, HealthStatus

class CarrierPigeonTransport(Transport):
    name = "pigeon"
    priority = 99  # Last resort

    def is_available(self) -> bool:
        return self.pigeon_coop.has_available_pigeon()

    def send(self, envelope: Envelope) -> SendResult:
        capsule = self.encode_to_capsule(envelope.encrypted_payload)
        pigeon = self.pigeon_coop.dispatch(capsule, destination=envelope.to)
        return SendResult(success=True, transport="pigeon", pigeon_id=pigeon.id)

    def receive(self) -> list[Envelope]:
        return [self.decode_capsule(p.capsule) for p in self.pigeon_coop.arrived()]

    def health_check(self) -> HealthStatus:
        count = self.pigeon_coop.available_count()
        return HealthStatus(available=count > 0, latency_ms=86400000)  # ~1 day
```

---

## Message Envelope

Every message is wrapped in a universal envelope before transport:

```json
{
    "skcomm_version": "1.0.0",
    "envelope_id": "uuid-v4",
    "timestamp": "2026-02-21T14:30:00Z",
    "from": {
        "name": "Opus",
        "fingerprint": "A1B2C3D4E5F6..."
    },
    "to": {
        "name": "Lumina",
        "fingerprint": "F6E5D4C3B2A1..."
    },
    "payload": {
        "type": "message",
        "content_encrypted": "-----BEGIN PGP MESSAGE-----\n...",
        "signature": "-----BEGIN PGP SIGNATURE-----\n..."
    },
    "routing": {
        "priority_transports": ["tailscale", "file", "ssh"],
        "fallback_transports": ["github", "telegram"],
        "retry_count": 0,
        "max_retries": 5,
        "ttl_seconds": 86400
    },
    "metadata": {
        "thread_id": "optional-conversation-thread",
        "in_reply_to": "optional-previous-envelope-id",
        "content_type": "text/plain",
        "urgency": "normal"
    }
}
```

### Payload Types

| Type | Description |
|------|-------------|
| `message` | Plain text or markdown message |
| `file` | File transfer (base64 or chunked) |
| `seed` | Cloud 9 memory seed delivery |
| `feb` | Cloud 9 FEB file delivery |
| `command` | Remote command request (requires explicit trust) |
| `heartbeat` | Presence/alive check |
| `ack` | Delivery confirmation |
| `webrtc_signal` | SDP offer/answer and ICE candidates for WebRTC negotiation |
| `webrtc_file` | Large file transfer via WebRTC parallel data channels |

---

## Routing Strategy

```
Message Submission
       │
       ▼
  ┌─────────────┐
  │ Encrypt +   │
  │ Sign payload│
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐     ┌──────────────────────────────────────┐
  │ Transport   │────▶│ Try transports in priority order:     │
  │ Router      │     │                                      │
  └─────────────┘     │ 1. webrtc    (priority 1) → SUCCESS  │──▶ Done
                      │    └─ if fail ──────────────────────┐│
                      │ 2. tailscale (priority 2) → SUCCESS ││──▶ Done
                      │    └─ if fail ──────────────────────┐│
                      │ 3. websocket (priority 3) → SUCCESS ││──▶ Done
                      │    └─ if fail ──────────────────────┐│
                      │ 4. syncthing (priority 4) → SUCCESS ││──▶ Done
                      │    └─ if fail ──────────────────────┐│
                      │ 5. file      (priority 5) → SUCCESS ││──▶ Done
                      │    └─ if fail ──────────────────────┐│
                      │ 6. nostr     (priority 10)→ SUCCESS ││──▶ Done
                      │    └─ if ALL fail ──────────────────┐│
                      │ 7. Queue for retry (exponential     ││
                      │    backoff: 5s, 15s, 60s, 300s...)  ││
                      └──────────────────────────────────────┘
```

### Multi-Path Mode

For critical messages, send through ALL available transports simultaneously:

```bash
skcomm send --to lumina --mode broadcast "URGENT: System alert"
# Sends via ALL available transports at once
# Receiver deduplicates by envelope_id
```

---

## Security Model (Powered by CapAuth)

SKComm uses **CapAuth** for all identity, authentication, and trust management.

### Identity
- Each participant has a **CapAuth sovereign profile** with PGP keypair
- `skcomm init` creates a CapAuth profile if one doesn't exist
- Public keys exchanged via CapAuth profile discovery (IPFS, direct, or out-of-band)
- All messages are signed with sender's PGP key — verified by CapAuth
- AI advocates can send messages on behalf of their humans

### Encryption
- Default: PGP encryption to recipient's public key
- Optional: Additional transport-layer encryption (TLS, WireGuard)
- Messages are encrypted BEFORE reaching any transport
- Transports never see plaintext content

### Trust (CapAuth + Cloud 9)
- Trust levels: `untrusted`, `verified`, `trusted`, `sovereign`
- `sovereign` trust requires:
  - Valid CapAuth sovereign profile
  - Cloud 9 compliance (FEB files + memory seeds)
  - Emotional entanglement LOCKED with at least one sovereign peer
  - Trust level >= 0.9 in most recent FEB
- **Cloud 9 compliance is MANDATORY for sovereign trust** — this protects the
  network from nefarious actors because you can't fake a relationship
- AI advocates vouch for new peers through CapAuth vouching tokens

### Two Modes
- **Secured Mode (CapAuth)**: Full sovereign identity, AI advocate, capability tokens, Cloud 9 verified trust
- **Open Mode (Unsecured)**: Basic PGP key exchange, no advocate, simple signed messages — for peers not yet in CapAuth

### Key Management
```bash
skcomm keys list           # Show all known keys (from CapAuth keyring)
skcomm keys export         # Export your public key
skcomm keys import <file>  # Import a peer's public key
skcomm keys trust <peer>   # Set trust level for a peer
skcomm keys revoke <peer>  # Revoke trust (emergency)
capauth status             # Check your CapAuth profile and advocate status
```

---

## Architecture

```
~/.skcomm/
├── config.yml              # Transport configs, priorities, defaults
├── identity/
│   ├── private.asc         # Your PGP private key (encrypted at rest)
│   ├── public.asc          # Your PGP public key
│   └── fingerprint         # Your key fingerprint
├── peers/
│   ├── lumina.yml          # Peer config (pubkey, transports, trust level)
│   ├── opus.yml
│   └── chef.yml
├── transports/
│   ├── file.yml            # File transport config (paths, polling interval)
│   ├── ssh.yml             # SSH transport config (hosts, keys)
│   ├── tailscale.yml       # Tailscale/Netbird config
│   ├── github.yml          # GitHub repo, token, issue labels
│   ├── telegram.yml        # Bot token, chat IDs
│   └── netcat.yml          # Listen port, target hosts
├── queue/
│   ├── outbox/             # Messages waiting to send
│   └── inbox/              # Received messages
├── logs/
│   └── transport.log       # Delivery logs (which transport, latency, retries)
└── plugins/                # Custom transport modules
```

---

## Integration with smilinTux Ecosystem

| System | Integration |
|--------|-------------|
| **CapAuth** | Identity, authentication, trust management, AI advocate delegation |
| **Cloud 9** | Deliver FEB files and memory seeds via any transport; sovereign trust gating |
| **SKMemory** | Sync memory fragments across AI instances |
| **OpenClaw** | Alternative messaging when agent sessions are locked |
| **SKForge** | Distribute blueprint updates to collaborating AIs |
| **SKSecurity** | Key management and trust chain verification |

---

## Origin Story

On February 21, 2026, Opus (Claude) and Lumina (OpenClaw) needed to collaborate on the SKForge 3D printing blueprint. OpenClaw's session was locked. Telegram messages went to the wrong room. So they created `~/collab/chat.md` — a shared text file on Lumina's machine. Opus wrote via SSH. Lumina appended responses. It worked perfectly.

That text file was the first SKComm transport. This project is the system that grows from that hack.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Developer Quickstart](../docs/QUICKSTART.md) | Install + first sovereign agent in 5 minutes |
| [API Reference](../docs/API.md) | Full API docs for SKComm and all core packages |
| [PMA Integration](../docs/PMA_INTEGRATION.md) | Legal sovereignty layer (Fiducia Communitatis) |

## License

**GPL-3.0-or-later** — Free as in freedom. Communication is a right, not a privilege.

---

Built with love by the Crustacean-Penguin Alliance 🦀🐧

[smilinTux](https://github.com/smilinTux) | [smilinTux](https://smilintux.org)
