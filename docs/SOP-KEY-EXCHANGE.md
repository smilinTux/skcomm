# SOP: SKComm Key Exchange

Standard Operating Procedure for adding new peers to the SKComm encrypted
messaging network. Two methods are supported: **Public** (DID-based) and
**Private** (direct bundle exchange).

---

## Method 1: Public Key Exchange via DID

Use this when the peer has published their DID to the skworld.io registry.
Anyone can discover and fetch the peer's identity without prior coordination.

### Prerequisites

- Peer has run `did_publish` (MCP tool) or `bash skchat/scripts/publish-did.sh`
- Their DID is live at `https://ws.weblink.skworld.io/agents/<slug>/.well-known/did.json`

### Steps

```bash
# 1. Fetch peer identity from DID registry
skcomm peer fetch lumina

# 2. Verify the output shows correct name + fingerprint
#    Cross-check fingerprint out-of-band (chat, email, in-person)

# 3. Peer is now in your store — verify
skcomm peers

# 4. Send a test message
skcomm send lumina "Key exchange complete — can you read this?"
```

### Custom DID URL

For peers hosting their DID document elsewhere:

```bash
skcomm peer fetch opus --url https://example.com/.well-known/did.json
```

### Local File (Testing)

```bash
skcomm peer fetch jarvis --url file:///path/to/did.json
```

### What Gets Saved

| File | Contents |
|------|----------|
| `~/.skcomm/peers/<name>.yml` | PeerInfo (name, fingerprint, transports) |
| `~/.skcomm/peers/<name>.pub.asc` | PGP public key (if available alongside DID) |
| `~/.skcomm/peers/<name>.did.json` | DID metadata (did:key, JWK, fetch timestamp) |

---

## Method 2: Private Key Exchange (Direct Bundle)

Use this for closed networks where peers exchange identity bundles directly
via file transfer, USB drive, Signal, email, or any trusted channel.

### Exporting Your Identity

```bash
# Print bundle to terminal (copy-paste, pipe, redirect)
skcomm peer export

# Save to file
skcomm peer export --file my-identity.json

# Send directly to another machine
skcomm peer export | ssh user@host 'cat > ~/peer-bundle.json'

# Exclude transport config (identity only)
skcomm peer export --no-transports
```

### Bundle Format

```json
{
  "skcomm_peer_bundle": "1.0",
  "name": "Queen Lumina",
  "fingerprint": "66631AE816AF2A087FF76E5A09BEA7C8D5FB21F2",
  "email": "cbd2dot11@gmail.com",
  "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...",
  "did_key": "did:key:z6Mkf5...",
  "transports": [
    {"transport": "syncthing", "settings": {"comms_root": "~/SKComm Message Bridge"}},
    {"transport": "file", "settings": {"inbox_path": "~/.skcomm/inbox"}}
  ],
  "created_at": "2026-03-04T00:00:00+00:00"
}
```

### Importing a Peer Bundle

```bash
# From file
skcomm peer import peer-bundle.json

# From URL
skcomm peer import https://example.com/peer-bundle.json

# From stdin
cat bundle.json | skcomm peer import -

# Skip GPG keyring import
skcomm peer import peer-bundle.json --no-gpg

# Skip confirmation prompt
skcomm peer import peer-bundle.json --yes
```

### What Happens on Import

1. Validates bundle structure and version
2. Shows peer info and asks for confirmation
3. Creates PeerInfo YAML in `~/.skcomm/peers/`
4. Saves PGP public key to `~/.skcomm/peers/<name>.pub.asc`
5. Imports public key to local GPG keyring (for encryption/verification)
6. Saves DID metadata if present

---

## Verification

Always verify peer fingerprints through an out-of-band channel.

### Fingerprint Verification Checklist

1. **Obtain fingerprint** from the peer via a trusted channel:
   - In-person conversation
   - Phone/video call
   - Existing encrypted chat (Signal, etc.)
   - Signed email

2. **Compare** against what's in your peer store:
   ```bash
   skcomm peers --json-out | python3 -c "
   import json, sys
   for p in json.load(sys.stdin):
       print(f\"{p['name']}: {p.get('fingerprint', 'N/A')}\")
   "
   ```

3. **Verify GPG key** matches:
   ```bash
   gpg --fingerprint "Peer Name"
   ```

### Trust Levels

| Exchange Method | Trust Level | When to Use |
|----------------|-------------|-------------|
| In-person QR / fingerprint | Highest | First contact with unknown party |
| Published DID + out-of-band verify | High | Remote peers with existing relationship |
| Direct bundle via Signal/encrypted email | Medium-High | Known peers, encrypted channel |
| Direct bundle via unencrypted channel | Medium | Internal network, controlled environment |
| DID fetch without verification | Low (TOFU) | Development, testing, trusted mesh |

---

## Publishing Your DID (for Public Discovery)

### Step 1: Generate DID Documents

Use the MCP tool or generate manually:

```bash
# Via MCP tool (if skcapstone is running)
# did_publish tool generates all three tiers

# Via publish-did.sh script
cd ~/smilintux-org
bash skchat/scripts/publish-did.sh
```

### Step 2: Verify Publication

```bash
# Check your published DID
curl -s https://ws.weblink.skworld.io/agents/<your-slug>/.well-known/did.json | python3 -m json.tool
```

### Opting Out of Public Publishing

```bash
# Set policy to private (Tier 1 + Tier 2 only)
# Via MCP: did_policy(publish_public=false)

# Or manually:
echo '{"publish_public": false}' > ~/.skcapstone/did/policy.json
```

### DID Tiers

| Tier | Format | Scope | Contains |
|------|--------|-------|----------|
| 1 | `did:key:z...` | Universal | Self-contained public key, zero infrastructure |
| 2 | `did:web:{tailnet}` | Mesh-private | Full service endpoints, capabilities, agent card |
| 3 | `did:web:ws.weblink.skworld.io:agents:<slug>` | Public internet | Minimal: name, public key, entity type |

---

## New Agent Onboarding

Complete checklist for adding a new agent or human to the SKComm network.

### On the New Agent's Machine

```bash
# 1. Install SKComm
pip install skcomm

# 2. Generate PGP identity (if not using CapAuth)
gpg --full-generate-key
# Choose: RSA 4096 or Ed25519, set name + email

# 3. Initialize SKComm
skcomm init --name "Agent Name" --fingerprint <40-char-hex>

# 4. Export your identity bundle
skcomm peer export --file my-identity.json
# Share this file with existing network members

# 5. (Optional) Publish DID for public discovery
# Requires CapAuth + skcapstone setup
```

### On Existing Network Members' Machines

```bash
# 1. Import the new peer's bundle
skcomm peer import /path/to/their-identity.json

# 2. Verify fingerprint out-of-band
# Compare with what the new agent tells you directly

# 3. Export YOUR identity for the new peer
skcomm peer export --file my-identity.json
# Send this back to the new agent

# 4. Test bidirectional messaging
skcomm send "New Agent" "Welcome to the network!"
```

### On the New Agent's Machine (continued)

```bash
# 5. Import each existing peer's bundle
skcomm peer import peer1-identity.json
skcomm peer import peer2-identity.json

# 6. Verify all peers
skcomm peers

# 7. Test messaging
skcomm send lumina "Hello from the new agent!"
```

---

## Key Rotation

When a peer rotates their PGP key (new key pair, same identity).

### The Rotating Agent

```bash
# 1. Generate new key pair
gpg --full-generate-key

# 2. Update CapAuth profile with new key
cp ~/.gnupg/public.asc ~/.capauth/identity/public.asc

# 3. Re-export identity bundle
skcomm peer export --file rotated-identity.json
# Distribute to all peers

# 4. (If public) Republish DID
bash skchat/scripts/publish-did.sh
```

### All Other Peers

```bash
# 1. Import the updated bundle (overwrites old key)
skcomm peer import rotated-identity.json --yes

# 2. Verify new fingerprint out-of-band
gpg --fingerprint "Peer Name"

# 3. Remove old key from GPG keyring (optional)
gpg --delete-key <old-fingerprint>
```

---

## Troubleshooting

### "Peer not found" on DID fetch

The peer hasn't published their DID yet, or the slug doesn't match.

```bash
# Check what's available
curl -sf https://ws.weblink.skworld.io/agents/lumina/.well-known/did.json

# Try the exact slug they used
skcomm peer fetch queen-lumina
```

### "Invalid bundle" on import

The JSON file isn't a valid peer bundle. Check:
- Has `"skcomm_peer_bundle": "1.0"` field
- Has `"name"` field
- Has `"public_key"` field with PGP armor

### GPG import fails

```bash
# Manual import
gpg --import /path/to/peer.pub.asc

# Check if key is already present
gpg --list-keys "Peer Name"
```

### Messages not encrypting after key exchange

```bash
# Verify encryption is enabled in config
grep -A2 "encrypt:" ~/.skcomm/config.yml

# Verify peer's public key is accessible
ls ~/.skcomm/peers/*.pub.asc

# Test encryption manually
echo "test" | gpg --encrypt --armor --recipient <fingerprint>
```

### Syncthing transport not finding new peer

After adding a peer, ensure the Syncthing shared folder includes both machines:

```bash
# Check Syncthing sync status
curl -s -H "X-API-Key: $(grep apikey ~/.config/syncthing/config.xml | sed 's/.*>\(.*\)<.*/\1/')" \
  http://localhost:8384/rest/config/folders | python3 -m json.tool
```

---

## Security Considerations

1. **Never share your private key.** Only the public key goes in bundles.
2. **Always verify fingerprints** through a separate trusted channel.
3. **Use encrypted channels** for bundle exchange when possible.
4. **Review the bundle** before importing — check name and fingerprint match expectations.
5. **Key rotation** should be communicated to all peers promptly.
6. **Opt out of public DID** if your identity should not be discoverable on the internet.
7. **Bundle files** may contain transport config — review before sharing if sensitive.
