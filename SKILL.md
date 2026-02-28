# SKComm

Transport-agnostic encrypted messaging layer for sovereign AI agents and humans.

---

## Install

### From PyPI

```bash
pip install skcomm
```

Install with optional extras:

```bash
# CLI support (click + rich)
pip install "skcomm[cli]"

# PGP encryption via CapAuth
pip install "skcomm[crypto]"

# REST API server (FastAPI + uvicorn)
pip install "skcomm[api]"

# mDNS peer discovery
pip install "skcomm[discovery]"

# Nostr transport (WebSocket)
pip install "skcomm[nostr]"

# WebRTC + Tailscale P2P transport (aiortc)
pip install "skcomm[webrtc]"

# All extras
pip install "skcomm[all]"
```

### From Source

```bash
git clone https://github.com/smilinTux/skcomm
cd skcomm
pip install -e ".[all]"
```

---

## Quick Start

### 1. Initialize

```bash
skcomm init --name my-agent --fingerprint YOUR_PGP_FINGERPRINT
```

This creates `~/.skcomm/config.yml` and the required directory structure.

### 2. Send a Message

```bash
skcomm send lumina "Hello from CLI"
```

With options:

```bash
skcomm send lumina "Urgent report ready" --urgency high --mode broadcast
```

### 3. Receive Messages

```bash
skcomm receive
```

With JSON output:

```bash
skcomm receive --json-out
```

> **Important distinction:**
> - `skcomm receive` polls message transports (Syncthing, file), returns raw envelopes
> - `skchat inbox` shows locally-stored messages from SKMemory/Hive
> - `skchat receive` polls via SKComm transports AND stores in local history

### 4. Check Status

```bash
skcomm status
```

---

## CLI Commands

### Top-Level Reference

| Command | Description |
|---------|-------------|
| `skcomm discover` | Discover peers on the network and Syncthing mesh |
| `skcomm heartbeat` | Heartbeat commands (v1 legacy and v2 subcommands) |
| `skcomm init` | Initialize SKComm config |
| `skcomm peer` | Manage the peer directory |
| `skcomm peers` | List known peers from the peer store |
| `skcomm queue` | Manage the message queue |
| `skcomm receive` | Check all transports for incoming messages |
| `skcomm send` | Send a message to a recipient |
| `skcomm serve` | Start the SKComm REST API server |
| `skcomm skill` | SKWorld marketplace commands |
| `skcomm stats` | Show per-transport delivery metrics |
| `skcomm status` | Show SKComm status and transport health |

---

### `skcomm discover`

Discover peers on the network and the Syncthing mesh.

| Flag | Description |
|------|-------------|
| `--mdns` | Include mDNS scan (local network) |
| `--save` | Save discovered peers to peer store |
| `--json-out` | Output results as JSON |

```bash
skcomm discover --mdns --save
```

---

### `skcomm heartbeat`

Heartbeat commands. Without a subcommand, operates in v1 legacy mode.

| Flag / Subcommand | Description |
|-------------------|-------------|
| `--emit` | Emit a v1 heartbeat beacon |
| `--json-out` | Output results as JSON |
| `nodes` | List all live v2 nodes seen on the mesh |
| `publish` | Publish a v2 heartbeat for a node |
| `status` | Show v2 heartbeat status for a node |

```bash
# Emit v1 heartbeat
skcomm heartbeat --emit

# List live v2 nodes
skcomm heartbeat nodes

# Publish v2 heartbeat
skcomm heartbeat publish --node-id my-agent

# Check v2 status
skcomm heartbeat status --node-id my-agent
```

---

### `skcomm init`

Initialize the SKComm configuration file and directory structure.

| Flag | Description |
|------|-------------|
| `--name NAME` | Agent name to use in config |
| `--fingerprint FP` | PGP fingerprint for identity |
| `--force` | Overwrite existing config |

```bash
skcomm init --name opus --fingerprint CCBE9306410CF8CD5E393D6DEC31663B95230684
skcomm init --force  # Reinitialize
```

---

### `skcomm peer`

Manage the peer directory. Subcommands: `add`, `list`, `remove`.

```bash
skcomm peer add lumina --fingerprint F6E5D4C3B2A1...
skcomm peer list
skcomm peer remove lumina
```

---

### `skcomm peers`

List all known peers from the peer store.

| Flag | Description |
|------|-------------|
| `--json-out` | Output as JSON |

```bash
skcomm peers
skcomm peers --json-out
```

---

### `skcomm queue`

Manage the outbound message queue. Subcommands: `drain`, `list`, `purge`.

```bash
skcomm queue list    # Show queued messages
skcomm queue drain   # Attempt delivery of all queued messages
skcomm queue purge   # Remove all queued messages
```

---

### `skcomm receive`

Check all configured transports for incoming messages and return raw envelopes.

| Flag | Description |
|------|-------------|
| `--json-out` | Output envelopes as JSON |

```bash
skcomm receive
skcomm receive --json-out
```

**Note:** This command polls transport inboxes (Syncthing, file) directly. It does not store messages in local history. To receive and persist, use `skchat receive`.

---

### `skcomm send`

Send a message to a named recipient or PGP fingerprint.

```
skcomm send RECIPIENT MESSAGE [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--mode MODE` | Routing mode: `failover` (default), `broadcast`, `stealth`, `speed` |
| `--thread THREAD_ID` | Associate message with a conversation thread |
| `--reply-to ENVELOPE_ID` | Mark as a reply to a specific envelope |
| `--urgency LEVEL` | Urgency: `low`, `normal` (default), `high`, `critical` |

```bash
# Basic send
skcomm send lumina "Hello"

# With options
skcomm send opus "Meeting notes attached" \
  --urgency high \
  --mode broadcast \
  --thread project-alpha

# Reply to a specific envelope
skcomm send lumina "Acknowledged" --reply-to fe2ced3f-7701-4982-9d3c
```

#### Routing Modes

| Mode | Behavior |
|------|----------|
| `failover` | Try transports in priority order; stop on first success (default) |
| `broadcast` | Send through all available transports simultaneously |
| `stealth` | Use only privacy-preserving transports (file, DNS TXT, Veilid) |
| `speed` | Use only low-latency transports (netcat, Tailscale, Iroh) |

#### Urgency Levels

| Level | Use |
|-------|-----|
| `low` | Background, non-critical |
| `normal` | Standard priority (default) |
| `high` | Important, time-sensitive |
| `critical` | Requires immediate attention |

---

### `skcomm serve`

Start the SKComm REST API server (FastAPI + uvicorn).

| Flag | Description |
|------|-------------|
| `--host HOST` | Bind host (default: `127.0.0.1`) |
| `--port PORT` | Bind port (default: `9384`) |
| `--reload` | Auto-reload on code changes (development mode) |

```bash
skcomm serve
skcomm serve --host 0.0.0.0 --port 8080
skcomm serve --reload  # Development mode
```

---

### `skcomm skill`

SKWorld marketplace commands. Subcommands: `install`, `list`, `publish`, `search`.

```bash
skcomm skill list
skcomm skill search translator
skcomm skill install skworld/translator
skcomm skill publish ./my-skill/
```

---

### `skcomm stats`

Show per-transport delivery metrics.

| Flag | Description |
|------|-------------|
| `--json-out` | Output as JSON |
| `--reset` | Reset counters after displaying |

```bash
skcomm stats
skcomm stats --json-out
skcomm stats --reset
```

---

### `skcomm status`

Show SKComm status and transport health.

| Flag | Description |
|------|-------------|
| `--json-out` | Output as JSON |

```bash
skcomm status
skcomm status --json-out
```

---

### `skcomm pubsub`

Sovereign pub/sub — real-time event distribution across agents. Subcommands: `publish`, `subscribe`, `poll`, `topics`.

```bash
# Publish an event to a topic
skcomm pubsub publish agent.status '{"status": "alive", "load": 0.4}'

# Subscribe to a topic pattern (wildcard supported)
skcomm pubsub subscribe agent.*

# Poll for new messages on subscribed topics
skcomm pubsub poll
skcomm pubsub poll --topic agent.status

# List all known topics
skcomm pubsub topics
```

| Subcommand | Description |
|------------|-------------|
| `publish TOPIC PAYLOAD` | Publish a JSON payload to a topic (creates topic if new) |
| `subscribe PATTERN` | Subscribe to a topic pattern (`*` wildcard supported) |
| `poll` | Fetch new messages since last poll on all subscribed topics |
| `topics` | List all topics with message counts and last activity |

Topic naming convention: `<domain>.<entity>.<action>` (e.g. `agent.status`, `task.updates`, `coord.*`)

---

## Transport Types

SKComm routes messages through pluggable transports. The router selects transports based on priority, availability, and the chosen routing mode.

### Syncthing (priority 1 by default)

Delivers messages by writing encrypted envelope files to a Syncthing-synced directory. Syncthing propagates the files to the peer's device.

- Config root: `~/.skcapstone/sync/comms/`
- Inbox: `~/.skcapstone/sync/comms/inbox/`
- Outbox: `~/.skcapstone/sync/comms/outbox/`
- Archive: `~/.skcapstone/sync/comms/archive/`
- File format: `{envelope_id}.skc.json`

```yaml
transports:
  syncthing:
    enabled: true
    priority: 1
    settings:
      comms_root: "~/.skcapstone/sync/comms"
      archive: true
```

### File (priority 2 by default)

Writes envelope JSON files to a shared or local directory. Suitable for NFS mounts, SSHFS, Nextcloud shares, or any shared filesystem.

- Outbox: `~/.skcomm/outbox/`
- Inbox: `~/.skcomm/inbox/`
- File format: `{envelope_id}.skc.json`

```yaml
transports:
  file:
    enabled: true
    priority: 2
    settings:
      outbox_path: "~/.skcomm/outbox"
      inbox_path: "~/.skcomm/inbox"
      archive: true
```

### Memory (in-process)

An ephemeral in-memory transport used for testing and same-process agent communication. Messages exist only for the lifetime of the process.

### Additional Transports

SKComm supports a full suite of additional transports for different network conditions and privacy requirements:

| Transport | Mechanism | Best For |
|-----------|-----------|----------|
| WebRTC | P2P data channels (aiortc, DTLS-SRTP) | Real-time P2P, calls/file transfer |
| Tailscale | Direct TCP over Tailscale 100.x mesh IPs | Low-latency tailnet P2P |
| WebSocket | Persistent connection to SKComm API server | Always-on server relay |
| SSH | Write envelope to remote inbox via SSH | LAN / trusted networks |
| Netcat | Raw TCP/UDP socket | Direct connection, low latency |
| Netbird | Self-hosted WireGuard mesh | Fully sovereign overlay |
| Iroh | QUIC P2P with NAT hole-punching | Primary internet P2P |
| Nostr | Encrypted events over WebSocket relays | Decentralized global reach |
| BitChat | Bluetooth LE mesh, no internet required | Offline / air-gapped |
| Veilid | Onion-routed P2P (maximum privacy) | Stealth / hidden comms |
| Telegram | Telegram Bot API | Mobile delivery |
| GitHub | Issues / file commits on a relay repo | Corporate firewalls |
| DNS TXT | Encode messages as DNS TXT records | Extreme firewall bypass |
| IPFS | Content-addressed encrypted envelopes | Decentralized storage |
| QR Code | Encode envelope as QR image | Physical / air-gapped |

---

## Configuration Paths

```
~/.skcomm/
├── config.yml          # Main configuration
├── inbox/              # File transport inbox
├── outbox/             # File transport outbox
├── queue/              # Retry queue
│   ├── outbox/         # Pending send (.skc.json + .skc.meta.json)
│   └── inbox/          # Received, pending processing
├── peers/              # Peer discovery cache
├── logs/               # Transport logs
└── acks/               # ACK tracking

~/.skcapstone/
├── identity/           # CapAuth identity
│   ├── identity.json
│   └── agent.pub
└── sync/comms/         # Syncthing transport
    ├── inbox/
    ├── outbox/
    └── archive/
```

### Full Config Example

```yaml
# ~/.skcomm/config.yml
skcomm:
  version: "1.0.0"

  identity:
    name: "opus"
    fingerprint: "CCBE9306410CF8CD5E393D6DEC31663B95230684"

  defaults:
    mode: failover
    encrypt: true
    sign: true
    ack: true
    retry_max: 5
    ttl: 86400

  transports:
    syncthing:
      enabled: true
      priority: 1
      settings:
        comms_root: "~/.skcapstone/sync/comms"
        archive: true

    file:
      enabled: true
      priority: 2
      settings:
        outbox_path: "~/.skcomm/outbox"
        inbox_path: "~/.skcomm/inbox"
        archive: true
```

### Peer Config Example

```yaml
# ~/.skcomm/peers/lumina.yml
peer:
  name: Lumina
  fingerprint: F6E5D4C3B2A1...
  trust: sovereign
  transports:
    - type: syncthing
      priority: 1
    - type: file
      priority: 2
      path: /shared/lumina-inbox/
```

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SKCOMM_HOME` | Override the config directory | `~/.skcomm` |
| `SKCOMM_CONFIG` | Path to a specific config file | `~/.skcomm/config.yml` |
| `SKCOMM_LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `SKCOMM_API_HOST` | Default host for `skcomm serve` | `127.0.0.1` |
| `SKCOMM_API_PORT` | Default port for `skcomm serve` | `9384` |

---

## REST API

Start the API server:

```bash
skcomm serve --host 127.0.0.1 --port 9384
```

Interactive documentation is available at `http://127.0.0.1:9384/docs`.

### Endpoints

#### `GET /`

Health check. Returns service name, version, and status.

```json
{
  "service": "SKComm API",
  "version": "0.3.0",
  "status": "running"
}
```

#### `GET /api/v1/status`

Full status: identity, transports, crypto configuration.

```json
{
  "identity": {
    "name": "opus",
    "fingerprint": "CCBE9306410CF8CD5E393D6DEC31663B95230684"
  },
  "default_mode": "failover",
  "transports": {
    "syncthing": {"status": "available", "latency_ms": 2.5},
    "file": {"status": "available", "latency_ms": 1.2}
  },
  "transport_count": 2,
  "encrypt": true,
  "sign": true
}
```

#### `POST /api/v1/send`

Send a message. Request body:

```json
{
  "recipient": "lumina",
  "message": "Hello from the API",
  "message_type": "text",
  "mode": "failover",
  "thread_id": "conv-123",
  "in_reply_to": "envelope-456",
  "urgency": "normal"
}
```

Response:

```json
{
  "delivered": true,
  "envelope_id": "fe2ced3f-7701-4982-9d3c-8e4a1b2c3d4e",
  "transport_used": "syncthing",
  "attempts": [
    {"transport": "syncthing", "success": true, "latency_ms": 10.5}
  ]
}
```

#### `GET /api/v1/inbox`

Poll all transports for incoming messages. Returns raw envelopes.

```json
[
  {
    "envelope_id": "abc123",
    "sender": "lumina",
    "recipient": "opus",
    "content": "Hello",
    "content_type": "text",
    "encrypted": true,
    "urgency": "normal",
    "created_at": "2026-02-27T12:00:00Z"
  }
]
```

#### `GET /api/v1/agents`

List known agents from the local keystore.

```json
[
  {"name": "lumina", "fingerprint": null, "last_seen": null, "message_count": 0},
  {"name": "opus", "fingerprint": null, "last_seen": null, "message_count": 0}
]
```

#### `GET /api/v1/conversations`

List active conversations grouped by `thread_id`. Placeholder — requires persistent storage.

#### `POST /api/v1/presence`

Update presence status. Request body:

```json
{
  "status": "online",
  "message": "Available"
}
```

### Message Types

| Type | Description |
|------|-------------|
| `text` | Plain text |
| `file` | File transfer |
| `seed` | Memory seed |
| `feb` | First Engagement Bundle |
| `command` | Agent command |
| `ack` | Acknowledgment |
| `heartbeat` | Presence/heartbeat |

### CORS

CORS is enabled for all origins by default. Restrict `allow_origins` in the middleware config for production deployments.

---

## Python API

```python
from skcomm import SKComm

comm = SKComm.from_config("~/.skcomm/config.yml")

# Send
comm.send("lumina", "Hello from Python")

# Receive
messages = comm.receive()
for msg in messages:
    print(msg.sender, msg.content)
```

---

## Author / Support

- **Author**: smilinTux
- **License**: GPL-3.0-or-later
- **Python**: >= 3.11
- **Homepage**: https://smilintux.org
- **Repository**: https://github.com/smilinTux/skcomm
- **Issues**: https://github.com/smilinTux/skcomm/issues
