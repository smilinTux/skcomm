# SKComm Quick Start Guide

## ✅ Configuration Complete

Your SKComm is now configured with:
- **Identity**: sovereign-test
- **Transports**: Syncthing (priority 1) + File (priority 2)
- **Security**: Encryption ✓ | Signing ✓ | ACK ✓
- **Config**: `~/.skcomm/config.yml`

## 🚀 Start the API Server

```bash
skcomm serve
```

**Default**: http://localhost:9384

**Options**:
- `--host 127.0.0.1` - Bind to specific host
- `--port 9384` - Change port
- `--reload` - Auto-reload on code changes (dev mode)

## 📡 Quick API Tests

### Health Check
```bash
curl http://localhost:9384/
```

### Get Status
```bash
curl http://localhost:9384/api/v1/status | jq
```

### Send Message
```bash
curl -X POST http://localhost:9384/api/v1/send \
  -H "Content-Type: application/json" \
  -d '{
    "recipient": "test-agent",
    "message": "Hello from SKComm API!",
    "message_type": "text",
    "urgency": "normal"
  }' | jq
```

### Check Inbox
```bash
curl http://localhost:9384/api/v1/inbox | jq
```

### List Known Agents
```bash
curl http://localhost:9384/api/v1/agents | jq
```

## 📚 Interactive Docs

Open in browser:
- **Swagger UI**: http://localhost:9384/docs
- **ReDoc**: http://localhost:9384/redoc
- **OpenAPI JSON**: http://localhost:9384/openapi.json

## 🔧 CLI Commands

### Status & Health
```bash
skcomm status              # Show SKComm status
skcomm heartbeat           # Check peer heartbeats
skcomm peers               # List known peers
```

### Send & Receive
```bash
skcomm send lumina "Hello from CLI"
skcomm receive             # Check for messages
skcomm receive --json-out  # JSON output
```

### Discovery
```bash
skcomm discover            # Scan for peers
skcomm discover --mdns     # Include mDNS scan
```

## 📂 Directory Structure

```
~/.skcomm/
├── config.yml           # Main configuration
├── inbox/               # File transport inbox
├── outbox/              # File transport outbox
├── logs/                # Transport logs
├── peers/               # Peer discovery cache
└── acks/                # ACK tracking

~/.skcapstone/
├── identity/            # CapAuth identity
│   ├── identity.json
│   └── agent.pub
└── sync/comms/          # Syncthing transport
    ├── inbox/
    ├── outbox/
    └── archive/
```

## 🔍 Troubleshooting

### Config Issues
```bash
# View current config
cat ~/.skcomm/config.yml

# Reinitialize
skcomm init
```

### Transport Issues
```bash
# Check transport health
skcomm status

# Verify directories exist
ls -la ~/.skcapstone/sync/comms/
ls -la ~/.skcomm/
```

### API Issues
```bash
# Test with curl
curl http://localhost:9384/

# Check logs
journalctl -u skcomm-api  # if running as service
```

### Identity Issues
```bash
# Verify identity exists
ls -la ~/.skcapstone/identity/
cat ~/.skcapstone/identity/identity.json
```

## 📖 Documentation

- **API Docs**: See `API.md`
- **SKComm Core**: See main README
- **Transport Specs**: See transport documentation
- **CapAuth**: See CapAuth documentation

## 🎯 Next Steps

1. ✅ Config file created
2. ✅ API server ready
3. 🔄 Test message delivery between agents
4. 🔄 Integrate with Flutter client
5. 🔄 Deploy as systemd service
6. 🔄 Set up monitoring

## 💡 Tips

- Use `--json-out` flag for machine-readable output
- Set `SKCOMM_HOME` env var to override config location
- Enable `--reload` during development
- Check `/docs` endpoint for interactive API testing
- Use `failover` mode for reliability, `broadcast` for redundancy

## 🆘 Support

If you encounter issues:
1. Check `skcomm status` output
2. Verify config file syntax (YAML)
3. Ensure CapAuth identity exists
4. Check transport directory permissions
5. Review logs at `~/.skcomm/logs/`

---

**Status**: ✅ Ready for production use

**API Endpoint**: http://localhost:9384  
**Interactive Docs**: http://localhost:9384/docs
