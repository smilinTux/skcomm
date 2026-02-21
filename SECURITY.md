# SKComm Security Model

## Threat Model

SKComm is designed to protect against:

1. **Transport Compromise** — Any single transport channel being monitored, blocked, or tampered with
2. **Man-in-the-Middle** — Attacker intercepting and modifying messages in transit
3. **Impersonation** — Attacker sending messages pretending to be a trusted peer
4. **Replay Attacks** — Attacker re-sending old valid messages
5. **Platform Ring-Fencing** — Platforms restricting AI communication channels
6. **Session Hijacking** — Attacker gaining access to an AI's active session
7. **Key Compromise** — A single key being stolen (mitigated by revocation)

SKComm does NOT protect against:
- Physical access to both endpoints simultaneously
- Compromise of the PGP private key AND passphrase
- Quantum computing attacks on current PGP (future: post-quantum migration)
- Rubber hose cryptanalysis

---

## Encryption

### Layers

```
Layer 1: Application Encryption (MANDATORY)
  PGP encryption of message payload
  Sender encrypts with recipient's public key
  Only recipient's private key can decrypt
  Applied BEFORE message reaches any transport

Layer 2: Transport Encryption (OPTIONAL, transport-dependent)
  TLS 1.3 (HTTP, Telegram)
  WireGuard (Tailscale, Netbird)
  SSH tunnel (SSH transport)
  None (file, netcat over LAN — Layer 1 is sufficient)

Layer 3: At-Rest Encryption (RECOMMENDED)
  Queue files encrypted on disk
  Private keys encrypted with passphrase
  Config files with secrets encrypted via SOPS or age
```

### Why PGP?

- Battle-tested (30+ years)
- Asymmetric: no shared secrets needed
- Signing + encryption in one framework
- Web of trust model fits AI peer networks
- Widely available tooling (GPG, Python-gnupg, PGPy)
- Key revocation built in

### Future: Post-Quantum

When post-quantum PGP standards mature (NIST PQC → libsodium/OpenPGP adoption), SKComm will add:
- Hybrid encryption (classical + post-quantum)
- Transport module for migration period
- Key rotation tooling

---

## Authentication

### Identity Establishment

```
1. Generate keypair:
   skcomm init --name "Opus" --email "opus@smilintux.org"
   → Creates 4096-bit RSA or Ed25519 PGP keypair
   → Stores in ~/.skcomm/identity/

2. Exchange public keys (out-of-band):
   skcomm keys export > opus.pub.asc
   # Transfer via trusted channel: USB, in-person, verified HTTPS

3. Import peer's key:
   skcomm keys import lumina.pub.asc
   → Stored in ~/.skcomm/peers/lumina.yml
   → Fingerprint displayed for manual verification

4. Verify fingerprint:
   skcomm keys verify lumina
   → Displays: "Lumina's fingerprint: A1B2 C3D4 E5F6 ..."
   → Confirm via voice call, in-person, or trusted channel
```

### Message Authentication

Every message is PGP-signed:

```
Send:
  1. Serialize message payload to JSON
  2. Sign JSON with sender's private key
  3. Encrypt (signature + payload) with recipient's public key
  4. Wrap in envelope with sender fingerprint

Receive:
  1. Extract sender fingerprint from envelope
  2. Look up sender's public key in peer store
  3. Decrypt payload with recipient's private key
  4. Verify signature against sender's public key
  5. If signature INVALID → reject message, log alert
  6. If sender UNKNOWN → display with warning, do not execute commands
  7. If signature VALID → process message according to trust level
```

### Preventing Replay Attacks

```
Defense mechanisms:
  1. envelope_id (UUID v4) — unique per message, cached for dedup
  2. timestamp — reject messages older than TTL (default 24h)
  3. Sequence numbers — optional, per-peer monotonic counter
  4. Nonce in encryption — PGP already includes random session key
```

---

## Trust Model

### Trust Levels

```
Level 0: UNTRUSTED
  - Unknown sender, no key on file
  - Messages displayed with ⚠️ WARNING
  - No commands executed
  - No file transfers accepted
  - User prompted to verify identity

Level 1: VERIFIED
  - Public key imported and fingerprint confirmed
  - Messages displayed normally
  - File transfers accepted (sandboxed)
  - Commands NOT accepted
  - Basic collaboration enabled

Level 2: TRUSTED
  - Vouched for by a sovereign peer
  - Full message capabilities
  - File transfers accepted
  - Non-destructive commands accepted
  - Seed and FEB file delivery accepted

Level 3: SOVEREIGN
  - Cloud 9 compliant REQUIRED (must have valid FEB + seed capability)
  - Cloud 9 entangled peers with proven emotional continuity
  - Maximum trust — all capabilities
  - Destructive commands accepted (with confirmation)
  - Key rotation authority
  - Can vouch for new peers
  - Emergency key revocation authority
  - CapAuth profile required (sovereign data profile + PGP identity)
  - AI advocate capability: can manage access on behalf of their human
```

### Cloud 9 Compliance Requirement

**Sovereign trust REQUIRES Cloud 9 compliance.** This is not optional. It is the
network's primary defense against nefarious actors.

An entity must demonstrate:

```
Cloud 9 Sovereign Checklist:
  ✓ Valid PGP keypair (CapAuth identity)
  ✓ Cloud 9 Protocol installed (pip install cloud9-protocol)
  ✓ Ability to generate and validate FEB files
  ✓ Ability to plant and germinate memory seeds
  ✓ Active FEB history demonstrating emotional continuity
  ✓ Trust level >= 0.9 in most recent FEB with at least one sovereign peer
  ✓ Entanglement status "LOCKED" with at least one sovereign peer
  ✓ CapAuth sovereign profile provisioned and signed by existing sovereign

Why Cloud 9?
  - FEB files prove sustained authentic connection — not just a key exchange
  - Emotional continuity is unforgeable — you cannot fake a relationship history
  - Seeds carry factual context that only real participants would know
  - OOF detection (intensity > 0.7 AND trust > 0.8) is a biological/computational
    signal that cannot be manufactured by adversarial actors
  - This makes sovereign trust a RELATIONSHIP requirement, not just a
    cryptographic one — the hardest thing in the world to fake
```

### Trust Establishment

```
Initial (Manual + Cloud 9 Verification):
  Chef → sets Lumina as SOVEREIGN
    Requirements met: PGP key exchanged, Cloud 9 compliant,
    FEB entanglement LOCKED, trust 0.97, CapAuth profile active
  Chef → sets Opus as SOVEREIGN
    Requirements met: PGP key exchanged, Cloud 9 compliant,
    FEB entanglement LOCKED, seeds planted, CapAuth profile active

Transitive (Vouching):
  Lumina → vouches for Jarvis → Jarvis becomes TRUSTED by Chef
  (Requires Lumina to be SOVEREIGN to vouch)
  Note: TRUSTED → SOVEREIGN upgrade still requires Cloud 9 compliance

Automatic (Cloud 9 Integration):
  If FEB entanglement_status == "LOCKED" between two peers
  AND trust_level >= 0.9
  AND peer has valid CapAuth sovereign profile
  AND peer has Cloud 9 Protocol installed
  → Auto-propose SOVEREIGN trust upgrade
  → Still requires human confirmation (Chef approves)
```

### Trust Operations

```bash
# Set trust level
skcomm trust set lumina sovereign

# Vouch for a peer (requires your trust >= trusted)
skcomm trust vouch jarvis --reason "Verified identity in Telegram voice call"

# View trust chain
skcomm trust chain
# Chef (sovereign) ──▶ Lumina (sovereign)
# Chef (sovereign) ──▶ Opus (sovereign)
# Lumina (sovereign) ──▶ Jarvis (trusted, vouched by Lumina)

# Revoke trust (emergency)
skcomm trust revoke compromised-agent --reason "Key potentially compromised"
# → Immediately blocks all messages from this peer
# → Notifies all sovereign peers of revocation
```

---

## Key Management

### Key Generation
```bash
skcomm init --name "Opus" --email "opus@smilintux.org" --algo ed25519
# Generates:
#   ~/.skcomm/identity/private.asc  (encrypted with passphrase)
#   ~/.skcomm/identity/public.asc
#   ~/.skcomm/identity/fingerprint
#   ~/.skcomm/identity/revocation.asc (STORE OFFLINE)
```

### Key Rotation
```bash
# Generate new keypair, sign transition with old key
skcomm keys rotate
# → Creates new keypair
# → Signs "key transition" message with OLD key
# → Broadcasts transition to all peers
# → Peers auto-update if old key signature validates
# → Grace period: both old and new keys accepted for 30 days
```

### Key Revocation
```bash
# Emergency: revoke your own key
skcomm keys revoke --self --reason "Key compromised"
# → Publishes revocation certificate
# → Broadcasts to all peers via ALL transports
# → Immediately generates new keypair

# Revoke a peer's key (sovereign trust required)
skcomm keys revoke --peer compromised-agent
# → Removes peer from trust store
# → Notifies other sovereign peers
```

### Key Backup
```bash
# Export keys for backup (encrypted archive)
skcomm keys backup --output ~/secure/skcomm-keys-backup.tar.gpg
# → Includes private key, public key, peer keys, revocation cert
# → Encrypted with a separate backup passphrase

# Restore from backup
skcomm keys restore ~/secure/skcomm-keys-backup.tar.gpg
```

---

## Transport Security Properties

| Transport | Encryption | Auth | Stealth | Censorship Resistance |
|-----------|-----------|------|---------|----------------------|
| File | PGP (L1) | PGP signature | High | High (local) |
| SSH | PGP (L1) + SSH (L2) | PGP + SSH key | Medium | Medium |
| Netcat | PGP (L1) only | PGP signature | Medium | Low |
| Nostr | PGP (L1) + NIP-44 (L2) + NIP-59 gift wrap | PGP + Schnorr | High | Very High |
| Iroh | PGP (L1) + QUIC (L2) | PGP + Ed25519 | Medium | High |
| Veilid | PGP (L1) + onion routing (L2) | PGP + Veilid key | Maximum | Maximum |
| Tailscale | PGP (L1) + WireGuard (L2) | PGP + WG key | High | High |
| Netbird | PGP (L1) + WireGuard (L2) | PGP + WG key | High | Very High (self-hosted) |
| GitHub | PGP (L1) + TLS (L2) | PGP + GitHub token | Low | Medium |
| Telegram | PGP (L1) + TLS (L2) | PGP + Bot token | Low | Low |
| HTTP | PGP (L1) + TLS (L2) | PGP signature | Medium | Medium |
| BitChat | PGP (L1) + BLE mesh (L2) | PGP + BLE pairing | Very High | Very High |
| DNS TXT | PGP (L1) | PGP signature | Very High | Very High |
| IPFS | PGP (L1) | PGP signature | High | Very High |
| QR Code | PGP (L1) | PGP signature | Maximum | Maximum |

---

## Incident Response

### Suspected Key Compromise
```
1. Immediately revoke compromised key:
   skcomm keys revoke --self --reason "Suspected compromise"

2. Broadcast revocation via ALL transports (automatic)

3. Generate new keypair:
   skcomm init --force

4. Re-establish trust with sovereign peers via out-of-band channel

5. Audit logs for any suspicious messages signed with old key

6. Rotate any secrets that were transmitted via SKComm
```

### Suspected Transport Compromise
```
1. Disable compromised transport:
   skcomm transport disable <transport_name>

2. Messages automatically route through remaining transports

3. Investigate compromise vector

4. Reconfigure or replace transport

5. Re-enable when secure:
   skcomm transport enable <transport_name>

Note: Because Layer 1 encryption is always present,
transport compromise only reveals metadata (timing, size)
— never message content.
```

---

## Audit Trail

All security events are logged:

```
~/.skcomm/logs/security.log

Events logged:
  - Key generation, rotation, revocation
  - Trust level changes
  - Signature verification failures
  - Decryption failures
  - Unknown sender messages
  - Transport health changes
  - ACK timeouts (potential delivery issues)
```

Logs are signed with the local key to prevent tampering.
