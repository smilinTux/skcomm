# CLAUDE.md — SKComm Developer Reference

AI agent working reference for the SKComm repository.
See README.md for the user-facing overview and docs/ for detailed SOPs.

---

## Install (venv — all SK* packages)

All SK* packages install into a shared virtualenv at `~/.skenv/` — never use
`pip install --user` or `--break-system-packages`.

```bash
# Quick install (creates ~/.skenv virtualenv)
bash scripts/install.sh

# Or manual:
python3 -m venv ~/.skenv
~/.skenv/bin/pip install -e ".[cli,crypto,discovery,api]"
export PATH="$HOME/.skenv/bin:$PATH"
```

Add the `PATH` export to `~/.bashrc` or `~/.zshrc` to persist it.

### Optional extras

```bash
~/.skenv/bin/pip install -e ".[webrtc]"   # WebRTC P2P transport (aiortc)
~/.skenv/bin/pip install -e ".[all]"      # Everything
```

---

## Repository Layout

```
skcomm/
├── src/skcomm/          # Python package source
│   ├── core.py          # SKComm orchestration entry point
│   ├── router.py        # Transport selection / failover
│   ├── crypto.py        # EnvelopeCrypto (PGP encrypt/decrypt)
│   ├── signing.py       # EnvelopeSigner (PGP sign/verify)
│   ├── key_exchange.py  # Peer key exchange: DID fetch + bundle export/import
│   ├── did_router.py    # FastAPI DID API endpoints (three-tier model)
│   ├── discovery.py     # PeerStore — YAML-backed peer registry
│   ├── cli.py           # Click CLI — skcomm commands
│   ├── api.py           # FastAPI app — skcomm serve
│   ├── mcp_server.py    # MCP server — skcomm-mcp
│   └── transports/      # Transport plugins (file, syncthing, websocket, ...)
├── docs/
│   ├── SOP-KEY-EXCHANGE.md   # Peer onboarding SOP
│   └── ARCHITECTURE.md       # Mermaid diagrams + endpoint reference
├── ARCHITECTURE.md      # Prose architecture + transport specs
├── README.md            # User-facing overview
├── SKILL.md             # Full CLI reference (agent skill card)
├── SECURITY.md          # Security model
└── pyproject.toml       # Package config + extras
```

---

## Key Modules

### `key_exchange.py`

Peer key exchange with two modes:

**Public — DID-based:**
```python
from skcomm.key_exchange import fetch_peer_from_did

# By agent slug (resolves to skworld.io DID registry)
peer = fetch_peer_from_did("lumina")

# By full URL
peer = fetch_peer_from_did("https://example.com/.well-known/did.json")

# Local file (testing)
peer = fetch_peer_from_did("file:///path/to/did.json")
```

**Private — peer bundles:**
```python
from skcomm.key_exchange import export_peer_bundle, import_peer_bundle

# Export own identity
bundle = export_peer_bundle(include_transports=True)
import json
json.dumps(bundle)  # write to file / send over Signal / etc.

# Import a peer's bundle
with open("peer-bundle.json") as f:
    bundle = json.load(f)
peer = import_peer_bundle(bundle, gpg_import=True)
```

The DID base URL is `https://ws.weblink.skworld.io/agents/{slug}/.well-known/did.json`.

### `did_router.py`

FastAPI `APIRouter` (`did_router`) with DID document endpoints.
Mounted in `api.py` on the main FastAPI app.

**Public endpoints (no auth):**
- `GET /.well-known/did.json` — Tier 2 mesh DID; falls back to Tier 1
- `GET /api/v1/did/key` — `did:key` identifier + PGP fingerprint
- `POST /api/v1/did/verify` — Structural DID challenge-response validation

**Authenticated endpoints (CapAuth bearer token):**
- `GET /api/v1/did/document` — All three DID tiers in one response
- `GET /api/v1/did/peers/{name}` — Peer DID from `~/.skcapstone/peers/{name}.json`
- `POST /api/v1/did/publish` — Generate all tiers, write to disk

Environment variables used by `did_router`:
- `SKWORLD_HOSTNAME` — Tailscale hostname for Tier 2 DID
- `SKWORLD_TAILNET` — Tailnet name for Tier 2 DID
- `SKCAPSTONE_HOME` — Override `~/.skcapstone/` (default)
- `SKCOMM_HOME` — Override `~/.skcomm/` (default)

---

## CLI Reference

### Core messaging

```bash
skcomm init --name "Opus" --fingerprint <40-hex>   # Create ~/.skcomm/config.yml
skcomm send lumina "Hello"                          # Send message
skcomm send lumina "Urgent!" --urgency high --mode broadcast
skcomm receive                                      # Poll all transports
skcomm status                                       # Transport health check
skcomm peers                                        # List known peers
```

### Peer key exchange

```bash
# Public: fetch peer from DID registry
skcomm peer fetch lumina
skcomm peer fetch opus --url https://example.com/.well-known/did.json
skcomm peer fetch jarvis --url file:///path/to/did.json

# Private: export own identity bundle
skcomm peer export
skcomm peer export --file my-identity.json
skcomm peer export --no-transports    # identity fields only

# Private: import a peer bundle
skcomm peer import peer-bundle.json
skcomm peer import https://example.com/peer-bundle.json
cat bundle.json | skcomm peer import -
skcomm peer import peer-bundle.json --no-gpg   # skip GPG keyring import
skcomm peer import peer-bundle.json --yes       # skip confirmation

# Legacy add
skcomm peer add lumina --fingerprint F6E5D4...
skcomm peer list
skcomm peer remove lumina
```

### Queue management

```bash
skcomm queue list     # Show pending messages
skcomm queue drain    # Retry delivery
skcomm queue purge    # Clear queue
```

### Server

```bash
skcomm serve                     # Start FastAPI server (default :8000)
SKCOMM_DEV_AUTH=1 skcomm serve   # Dev mode: disable CapAuth signature check
```

---

## Peer Bundle Format

```json
{
  "skcomm_peer_bundle": "1.0",
  "name": "Queen Lumina",
  "fingerprint": "66631AE816AF2A087FF76E5A09BEA7C8D5FB21F2",
  "email": "cbd2dot11@gmail.com",
  "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...",
  "did_key": "did:key:z6Mkf5...",
  "transports": [
    {"transport": "syncthing", "settings": {"comms_root": "~/.skcapstone/comms"}},
    {"transport": "file", "settings": {"inbox_path": "~/.skcomm/inbox"}}
  ],
  "created_at": "2026-03-04T00:00:00+00:00"
}
```

---

## DID Three-Tier Model

| Tier | DID Format | Scope | Contents |
|------|-----------|-------|----------|
| 1 | `did:key:z6Mk...` | Universal | Self-contained, zero infrastructure |
| 2 | `did:web:{tailnet-hostname}` | Mesh-private | Full endpoints, capabilities, agent card |
| 3 | `did:web:ws.weblink.skworld.io:agents:{slug}` | Public internet | Minimal: name + JWK |

Files written by `POST /api/v1/did/publish`:
- `~/.skcomm/well-known/did.json` — Tier 2 (mesh), served via `tailscale serve`
- `~/.skcapstone/did/key.json` — Tier 1 (`did:key`)
- `~/.skcapstone/did/public.json` — Tier 3 (public)
- `~/.skcapstone/did/did_key.txt` — plain `did:key` string

---

## Data Directories

```
~/.skcomm/
├── config.yml              # Transport configs, identity, defaults
├── peers/
│   ├── lumina.yml          # PeerInfo YAML
│   ├── lumina.pub.asc      # PGP public key
│   └── lumina.did.json     # DID metadata (did:key, JWK, fetch timestamp)
├── queue/outbox/           # Pending messages
├── queue/inbox/            # Received, pending processing
└── well-known/did.json     # Tier 2 DID document (served via tailscale serve)

~/.skcapstone/
├── did/
│   ├── did_key.txt         # Plain did:key string
│   ├── key.json            # Tier 1 DID document
│   └── public.json         # Tier 3 DID document
└── peers/
    └── {name}.json         # Peer data (used by DID router)

~/.capauth/identity/
├── public.asc              # PGP public key (read by export_peer_bundle)
└── profile.json            # CapAuth profile (name, fingerprint, entity)
```

---

## Environment Variables

| Variable | Effect |
|----------|--------|
| `SKCOMM_HOME` | Override `~/.skcomm/` directory |
| `SKCAPSTONE_HOME` | Override `~/.skcapstone/` directory |
| `SKCOMM_DEV_AUTH` | Set `1` to disable CapAuth signature check (dev only) |
| `SKWORLD_HOSTNAME` | Tailscale hostname for Tier 2 DID generation |
| `SKWORLD_TAILNET` | Tailnet name for Tier 2 DID generation |
| `SKCOMM_TURN_SECRET` | HMAC secret for WebRTC TURN credential generation |

---

## Key Exchange SOP

See [docs/SOP-KEY-EXCHANGE.md](docs/SOP-KEY-EXCHANGE.md) for:
- Step-by-step public (DID) key exchange
- Step-by-step private (bundle) key exchange
- Fingerprint verification checklist
- New agent onboarding checklist
- Key rotation procedure
- Troubleshooting

---

## Testing

```bash
cd /home/cbrd21/clawd/pillar-repos/skcomm
~/.skenv/bin/pytest
~/.skenv/bin/pytest tests/test_key_exchange.py -v
```
