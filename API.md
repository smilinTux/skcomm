# SKComm REST API

FastAPI server that wraps the SKComm Python API and exposes HTTP endpoints for Flutter/desktop clients.

## Quick Start

### 1. Install SKComm with API support

```bash
pip install -e ".[api,crypto,cli]"
```

### 2. Configure SKComm

Create `~/.skcomm/config.yml`:

```yaml
skcomm:
  version: "1.0.0"
  
  identity:
    name: "your-agent-name"
    fingerprint: "YOUR_PGP_FINGERPRINT"
  
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

### 3. Start the API server

```bash
skcomm serve --host 127.0.0.1 --port 9384
```

Or for development with auto-reload:

```bash
skcomm serve --reload
```

### 4. Access the API

- **API Base URL**: http://127.0.0.1:9384
- **Interactive Docs**: http://127.0.0.1:9384/docs
- **OpenAPI Schema**: http://127.0.0.1:9384/openapi.json

## API Endpoints

### Health Check

**GET /**

Root endpoint for health checks.

**Response:**
```json
{
  "service": "SKComm API",
  "version": "0.1.0",
  "status": "running"
}
```

### Get Status

**GET /api/v1/status**

Get the current status of SKComm including identity, transports, and crypto configuration.

**Response:**
```json
{
  "version": "1.0.0",
  "identity": {
    "name": "sovereign-test",
    "fingerprint": "CCBE9306410CF8CD5E393D6DEC31663B95230684"
  },
  "default_mode": "failover",
  "transports": {
    "syncthing": {
      "status": "available",
      "latency_ms": 2.5
    },
    "file": {
      "status": "available",
      "latency_ms": 1.2
    }
  },
  "transport_count": 2,
  "encrypt": true,
  "sign": true,
  "crypto": {
    "available": true,
    "encrypt_enabled": true,
    "sign_enabled": true,
    "fingerprint": "CCBE9306410CF8CD5E393D6DEC31663B95230684",
    "known_peers": ["lumina", "opus"]
  }
}
```

### Send Message

**POST /api/v1/send**

Send a message to a recipient through available transports.

**Request Body:**
```json
{
  "recipient": "lumina",
  "message": "Hello from the SKComm API!",
  "message_type": "text",
  "mode": "failover",
  "thread_id": "conversation-123",
  "in_reply_to": "envelope-456",
  "urgency": "normal"
}
```

**Response:**
```json
{
  "delivered": true,
  "envelope_id": "fe2ced3f-7701-4982-9d3c-8e4a1b2c3d4e",
  "transport_used": "syncthing",
  "attempts": [
    {
      "transport": "syncthing",
      "success": true,
      "latency_ms": 10.5,
      "error": null
    }
  ]
}
```

### Get Inbox

**GET /api/v1/inbox**

Check all transports for incoming messages.

**Response:**
```json
[
  {
    "envelope_id": "abc123",
    "sender": "opus",
    "recipient": "sovereign-test",
    "content": "Hello from Opus!",
    "content_type": "text",
    "encrypted": true,
    "compressed": false,
    "signature": "-----BEGIN PGP SIGNATURE-----...",
    "thread_id": "conv-789",
    "in_reply_to": null,
    "urgency": "normal",
    "created_at": "2026-02-24T12:00:00Z",
    "is_ack": false
  }
]
```

### Get Conversations

**GET /api/v1/conversations**

Get a list of active conversations grouped by thread_id.

**Response:**
```json
[]
```

*Note: This is a placeholder endpoint. Full conversation management requires persistent storage.*

### Get Known Agents

**GET /api/v1/agents**

Get a list of known agents from the local keystore.

**Response:**
```json
[
  {
    "name": "lumina",
    "fingerprint": null,
    "last_seen": null,
    "message_count": 0
  },
  {
    "name": "opus",
    "fingerprint": null,
    "last_seen": null,
    "message_count": 0
  }
]
```

### Update Presence

**POST /api/v1/presence**

Update presence status.

**Request Body:**
```json
{
  "status": "online",
  "message": "Working on SKComm API"
}
```

**Response:**
```json
{
  "status": "online",
  "message": "Working on SKComm API",
  "updated_at": "2026-02-24T12:00:00",
  "identity": "sovereign-test"
}
```

*Note: This is a placeholder endpoint. Full presence management requires a heartbeat system.*

## Message Types

- `text` - Plain text messages
- `file` - File transfers
- `seed` - Memory seeds
- `feb` - FEB (First Engagement Bundle)
- `command` - Command messages
- `ack` - Acknowledgment messages
- `heartbeat` - Heartbeat/presence messages

## Routing Modes

- `failover` - Try transports in priority order until one succeeds (default)
- `broadcast` - Send through all available transports
- `stealth` - Use only stealth/privacy-focused transports
- `speed` - Use only fast/realtime transports

## Urgency Levels

- `low` - Background/non-critical messages
- `normal` - Standard priority (default)
- `high` - Important messages
- `critical` - Urgent messages requiring immediate attention

## Development

### Run Tests

```bash
pytest tests/test_api.py -v
```

### Run with Auto-Reload

```bash
skcomm serve --reload
```

### Access Interactive Docs

Open http://127.0.0.1:9384/docs in your browser to access the FastAPI interactive documentation.

## CORS

The API has CORS enabled for all origins (`*`). For production deployments, configure `allow_origins` in the middleware settings to restrict access to specific domains.

## Security

- All messages are encrypted and signed by default (when crypto is enabled)
- PGP keys are managed by CapAuth
- Identity fingerprints are verified on message receipt
- Messages expire based on TTL configuration

## Architecture

The API server wraps the existing SKComm Python API:

```
Flutter/Desktop Client
        ↓
   HTTP/REST API (FastAPI)
        ↓
   SKComm.from_config()
        ↓
   Router + Transports
        ↓
   Syncthing / File / Nostr
```

## Troubleshooting

### Port Already in Use

```bash
# Change the port
skcomm serve --port 8080
```

### Config Not Found

Ensure `~/.skcomm/config.yml` exists and is properly formatted. Run:

```bash
skcomm init
```

### Transport Unavailable

Check transport health:

```bash
skcomm status
```

### Crypto Errors

Verify CapAuth identity exists:

```bash
ls -la ~/.skcapstone/identity/
```
