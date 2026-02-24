"""Tests for SKComm peer discovery and PeerStore."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from skcomm.discovery import (
    ENVELOPE_SUFFIX,
    PeerInfo,
    PeerStore,
    PeerTransport,
    discover_all,
    discover_file_transport,
    discover_syncthing,
)


def _write_envelope(directory: Path, sender: str, envelope_id: str = "test-001") -> Path:
    """Write a minimal envelope file to a directory."""
    directory.mkdir(parents=True, exist_ok=True)
    data = json.dumps({
        "skcomm_version": "1.0.0",
        "envelope_id": envelope_id,
        "sender": sender,
        "recipient": "test-agent",
        "payload": {"content": "hello", "content_type": "text"},
    })
    path = directory / f"{envelope_id}{ENVELOPE_SUFFIX}"
    path.write_text(data)
    return path


# ═══════════════════════════════════════════════════════════
# PeerInfo model
# ═══════════════════════════════════════════════════════════


class TestPeerInfo:
    """Test the PeerInfo pydantic model."""

    def test_basic_creation(self):
        peer = PeerInfo(name="lumina")
        assert peer.name == "lumina"
        assert peer.fingerprint is None
        assert peer.transports == []

    def test_with_transports(self):
        peer = PeerInfo(
            name="opus",
            fingerprint="ABCD1234" * 5,
            transports=[PeerTransport(transport="syncthing", settings={"root": "/tmp"})],
        )
        assert len(peer.transports) == 1
        assert peer.transports[0].transport == "syncthing"

    def test_merge_fills_gaps(self):
        a = PeerInfo(name="lumina", fingerprint="FP123")
        b = PeerInfo(
            name="lumina",
            nostr_pubkey="npub_abc",
            transports=[PeerTransport(transport="nostr", settings={"relay": "wss://r"})],
        )
        merged = a.merge(b)
        assert merged.fingerprint == "FP123"
        assert merged.nostr_pubkey == "npub_abc"
        assert len(merged.transports) == 1

    def test_merge_combines_transports(self):
        a = PeerInfo(
            name="opus",
            transports=[PeerTransport(transport="syncthing", settings={"root": "/a"})],
        )
        b = PeerInfo(
            name="opus",
            transports=[PeerTransport(transport="file", settings={"inbox": "/b"})],
        )
        merged = a.merge(b)
        names = {t.transport for t in merged.transports}
        assert names == {"syncthing", "file"}

    def test_merge_keeps_latest_seen(self):
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 2, 1, tzinfo=timezone.utc)
        a = PeerInfo(name="x", last_seen=old)
        b = PeerInfo(name="x", last_seen=new)
        assert a.merge(b).last_seen == new


# ═══════════════════════════════════════════════════════════
# PeerStore persistence
# ═══════════════════════════════════════════════════════════


class TestPeerStore:
    """Test YAML-based peer storage."""

    def test_add_and_get(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        peer = PeerInfo(name="lumina", fingerprint="FP1234")
        store.add(peer)

        loaded = store.get("lumina")
        assert loaded is not None
        assert loaded.name == "lumina"
        assert loaded.fingerprint == "FP1234"

    def test_get_nonexistent(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        assert store.get("ghost") is None

    def test_list_all(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        store.add(PeerInfo(name="alice"))
        store.add(PeerInfo(name="bob"))
        store.add(PeerInfo(name="charlie"))
        peers = store.list_all()
        assert len(peers) == 3
        assert [p.name for p in peers] == ["alice", "bob", "charlie"]

    def test_list_empty_store(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        assert store.list_all() == []

    def test_remove(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        store.add(PeerInfo(name="temp"))
        assert store.remove("temp") is True
        assert store.get("temp") is None

    def test_remove_nonexistent(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        assert store.remove("ghost") is False

    def test_add_merges_existing(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        store.add(PeerInfo(name="lumina", fingerprint="FP"))
        store.add(PeerInfo(
            name="lumina",
            nostr_pubkey="npub_xyz",
            transports=[PeerTransport(transport="nostr", settings={"relay": "wss://r"})],
        ))
        loaded = store.get("lumina")
        assert loaded.fingerprint == "FP"
        assert loaded.nostr_pubkey == "npub_xyz"
        assert len(loaded.transports) == 1

    def test_yaml_file_is_valid(self, tmp_path):
        store = PeerStore(peers_dir=tmp_path / "peers")
        store.add(PeerInfo(name="opus", fingerprint="ABC123"))
        path = tmp_path / "peers" / "opus.yml"
        assert path.exists()
        data = yaml.safe_load(path.read_text())
        assert data["name"] == "opus"
        assert data["fingerprint"] == "ABC123"


# ═══════════════════════════════════════════════════════════
# Syncthing discovery
# ═══════════════════════════════════════════════════════════


class TestSyncthingDiscovery:
    """Test Syncthing comms directory scanning."""

    def test_discovers_inbox_peers(self, tmp_path):
        comms = tmp_path / "comms"
        (comms / "inbox" / "lumina").mkdir(parents=True)
        (comms / "inbox" / "opus").mkdir(parents=True)

        peers = discover_syncthing(comms)
        names = {p.name for p in peers}
        assert names == {"lumina", "opus"}

    def test_discovers_outbox_peers(self, tmp_path):
        comms = tmp_path / "comms"
        (comms / "outbox" / "jarvis").mkdir(parents=True)

        peers = discover_syncthing(comms)
        assert len(peers) == 1
        assert peers[0].name == "jarvis"

    def test_merges_inbox_and_outbox(self, tmp_path):
        comms = tmp_path / "comms"
        (comms / "inbox" / "lumina").mkdir(parents=True)
        (comms / "outbox" / "lumina").mkdir(parents=True)

        peers = discover_syncthing(comms)
        assert len(peers) == 1
        assert peers[0].name == "lumina"

    def test_extracts_last_seen_from_envelopes(self, tmp_path):
        comms = tmp_path / "comms"
        peer_dir = comms / "inbox" / "lumina"
        _write_envelope(peer_dir, "lumina", "env-001")

        peers = discover_syncthing(comms)
        assert len(peers) == 1
        assert peers[0].last_seen is not None

    def test_skips_hidden_dirs(self, tmp_path):
        comms = tmp_path / "comms"
        (comms / "inbox" / ".stfolder").mkdir(parents=True)
        (comms / "inbox" / "legit").mkdir(parents=True)

        peers = discover_syncthing(comms)
        assert len(peers) == 1
        assert peers[0].name == "legit"

    def test_empty_comms_returns_empty(self, tmp_path):
        assert discover_syncthing(tmp_path / "nonexistent") == []

    def test_transport_config_included(self, tmp_path):
        comms = tmp_path / "comms"
        (comms / "inbox" / "opus").mkdir(parents=True)

        peers = discover_syncthing(comms)
        assert len(peers[0].transports) == 1
        assert peers[0].transports[0].transport == "syncthing"
        assert peers[0].transports[0].settings["comms_root"] == str(comms)

    def test_extracts_fingerprint_from_envelope(self, tmp_path):
        comms = tmp_path / "comms"
        peer_dir = comms / "inbox" / "agent-x"
        fp = "a1b2c3d4e5" * 4  # 40-char hex fingerprint
        _write_envelope(peer_dir, fp, "fp-env")

        peers = discover_syncthing(comms)
        assert peers[0].fingerprint == fp


# ═══════════════════════════════════════════════════════════
# File transport discovery
# ═══════════════════════════════════════════════════════════


class TestFileTransportDiscovery:
    """Test file transport inbox/outbox scanning."""

    def test_discovers_from_inbox(self, tmp_path):
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        _write_envelope(inbox, "lumina", "file-001")

        peers = discover_file_transport(inbox_path=inbox, outbox_path=outbox)
        assert len(peers) == 1
        assert peers[0].name == "lumina"
        assert peers[0].discovered_via == "file"

    def test_multiple_senders(self, tmp_path):
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        _write_envelope(inbox, "lumina", "msg-001")
        _write_envelope(inbox, "opus", "msg-002")

        peers = discover_file_transport(inbox_path=inbox, outbox_path=outbox)
        names = {p.name for p in peers}
        assert names == {"lumina", "opus"}

    def test_empty_inbox_returns_empty(self, tmp_path):
        peers = discover_file_transport(
            inbox_path=tmp_path / "nope",
            outbox_path=tmp_path / "also_nope",
        )
        assert peers == []

    def test_skips_invalid_json(self, tmp_path):
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(parents=True)
        (inbox / f"bad{ENVELOPE_SUFFIX}").write_text("not json")
        _write_envelope(inbox, "valid", "good-001")

        peers = discover_file_transport(inbox_path=inbox, outbox_path=outbox)
        assert len(peers) == 1
        assert peers[0].name == "valid"


# ═══════════════════════════════════════════════════════════
# Combined discovery
# ═══════════════════════════════════════════════════════════


class TestDiscoverAll:
    """Test the combined discovery function."""

    def test_merges_syncthing_and_file(self, tmp_path):
        comms = tmp_path / "comms"
        (comms / "inbox" / "lumina").mkdir(parents=True)

        inbox = tmp_path / "inbox"
        _write_envelope(inbox, "lumina", "file-merge-001")

        peers = discover_all(
            comms_root=comms,
            inbox_path=inbox,
            outbox_path=tmp_path / "outbox",
            skip_mdns=True,
        )
        assert len(peers) == 1
        assert peers[0].name == "lumina"
        transports = {t.transport for t in peers[0].transports}
        assert "syncthing" in transports
        assert "file" in transports

    def test_deduplicates_by_name(self, tmp_path):
        comms = tmp_path / "comms"
        (comms / "inbox" / "opus").mkdir(parents=True)
        (comms / "outbox" / "opus").mkdir(parents=True)

        inbox = tmp_path / "inbox"
        _write_envelope(inbox, "opus", "dedup-001")

        peers = discover_all(
            comms_root=comms,
            inbox_path=inbox,
            outbox_path=tmp_path / "outbox",
            skip_mdns=True,
        )
        assert len(peers) == 1

    def test_empty_returns_empty(self, tmp_path):
        peers = discover_all(
            comms_root=tmp_path / "a",
            inbox_path=tmp_path / "b",
            outbox_path=tmp_path / "c",
            skip_mdns=True,
        )
        assert peers == []

    def test_sorted_by_name(self, tmp_path):
        comms = tmp_path / "comms"
        for name in ["charlie", "alice", "bob"]:
            (comms / "inbox" / name).mkdir(parents=True)

        peers = discover_all(
            comms_root=comms,
            inbox_path=tmp_path / "inbox",
            outbox_path=tmp_path / "outbox",
            skip_mdns=True,
        )
        assert [p.name for p in peers] == ["alice", "bob", "charlie"]
