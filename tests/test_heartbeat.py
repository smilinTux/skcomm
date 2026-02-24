"""Tests for the SKComm heartbeat protocol — alive/dead peer detection."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcomm.heartbeat import (
    HEARTBEAT_SUFFIX,
    HeartbeatMonitor,
    HeartbeatPayload,
    PeerHeartbeat,
    PeerLiveness,
)


def _write_heartbeat(
    hb_dir: Path,
    agent: str,
    age_seconds: float = 0,
    transports: list[str] | None = None,
    fingerprint: str | None = None,
) -> Path:
    """Write a heartbeat file with a configurable age."""
    hb_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    payload = HeartbeatPayload(
        agent=agent,
        timestamp=ts,
        transports=transports or [],
        fingerprint=fingerprint,
    )
    path = hb_dir / f"{agent}{HEARTBEAT_SUFFIX}"
    path.write_text(payload.model_dump_json(indent=2))
    return path


# ═══════════════════════════════════════════════════════════
# HeartbeatPayload model
# ═══════════════════════════════════════════════════════════


class TestHeartbeatPayload:
    """Test the heartbeat data model."""

    def test_basic_payload(self):
        hb = HeartbeatPayload(agent="opus")
        assert hb.agent == "opus"
        assert hb.version == "1.0.0"
        assert hb.timestamp is not None

    def test_payload_with_metadata(self):
        hb = HeartbeatPayload(
            agent="lumina",
            fingerprint="ABC123",
            nostr_pubkey="npub_xyz",
            transports=["syncthing", "nostr"],
        )
        assert hb.fingerprint == "ABC123"
        assert len(hb.transports) == 2

    def test_serialization_roundtrip(self):
        hb = HeartbeatPayload(agent="jarvis", transports=["file"])
        data = hb.model_dump_json()
        loaded = HeartbeatPayload.model_validate_json(data)
        assert loaded.agent == "jarvis"
        assert loaded.transports == ["file"]


# ═══════════════════════════════════════════════════════════
# HeartbeatMonitor — emit
# ═══════════════════════════════════════════════════════════


class TestHeartbeatEmit:
    """Test heartbeat emission."""

    def test_emit_creates_file(self, tmp_path):
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        path = monitor.emit()
        assert path.exists()
        assert path.name == f"opus{HEARTBEAT_SUFFIX}"

    def test_emit_contains_agent_name(self, tmp_path):
        monitor = HeartbeatMonitor("lumina", comms_root=tmp_path)
        monitor.emit()
        path = tmp_path / "heartbeats" / f"lumina{HEARTBEAT_SUFFIX}"
        payload = HeartbeatPayload.model_validate_json(path.read_text())
        assert payload.agent == "lumina"

    def test_emit_includes_metadata(self, tmp_path):
        monitor = HeartbeatMonitor(
            "opus",
            comms_root=tmp_path,
            fingerprint="FP123",
            nostr_pubkey="npub_abc",
            transports=["syncthing", "nostr"],
        )
        monitor.emit()
        path = tmp_path / "heartbeats" / f"opus{HEARTBEAT_SUFFIX}"
        payload = HeartbeatPayload.model_validate_json(path.read_text())
        assert payload.fingerprint == "FP123"
        assert payload.nostr_pubkey == "npub_abc"
        assert payload.transports == ["syncthing", "nostr"]

    def test_emit_overwrites_previous(self, tmp_path):
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        monitor.emit()
        time.sleep(0.05)
        path2 = monitor.emit()
        payload = HeartbeatPayload.model_validate_json(path2.read_text())
        age = (datetime.now(timezone.utc) - payload.timestamp).total_seconds()
        assert age < 2

    def test_emit_atomic_write(self, tmp_path):
        """No .tmp files should remain after emit."""
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        monitor.emit()
        hb_dir = tmp_path / "heartbeats"
        tmp_files = list(hb_dir.glob(".*"))
        assert len(tmp_files) == 0


# ═══════════════════════════════════════════════════════════
# HeartbeatMonitor — read and classify
# ═══════════════════════════════════════════════════════════


class TestHeartbeatClassification:
    """Test peer liveness classification."""

    def test_alive_peer(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "lumina", age_seconds=30)
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)

        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.ALIVE
        assert status.age_seconds is not None
        assert status.age_seconds < 35

    def test_stale_peer(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "lumina", age_seconds=180)
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)

        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.STALE

    def test_dead_peer(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "lumina", age_seconds=600)
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)

        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.DEAD

    def test_unknown_peer(self, tmp_path):
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        status = monitor.peer_status("ghost")
        assert status.status == PeerLiveness.UNKNOWN

    def test_custom_timeouts(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "lumina", age_seconds=50)
        monitor = HeartbeatMonitor(
            "opus", comms_root=tmp_path,
            alive_timeout=30, stale_timeout=60,
        )
        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.STALE

    def test_status_includes_transports(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "lumina", transports=["syncthing", "file"])
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        status = monitor.peer_status("lumina")
        assert status.transports == ["syncthing", "file"]

    def test_status_includes_fingerprint(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "lumina", fingerprint="FP_LUMINA")
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        status = monitor.peer_status("lumina")
        assert status.fingerprint == "FP_LUMINA"


# ═══════════════════════════════════════════════════════════
# HeartbeatMonitor — scan
# ═══════════════════════════════════════════════════════════


class TestHeartbeatScan:
    """Test scanning all peer heartbeats."""

    def test_scan_multiple_peers(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "alice", age_seconds=10)
        _write_heartbeat(hb_dir, "bob", age_seconds=200)
        _write_heartbeat(hb_dir, "charlie", age_seconds=600)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()

        assert len(results) == 3
        by_name = {r.name: r.status for r in results}
        assert by_name["alice"] == PeerLiveness.ALIVE
        assert by_name["bob"] == PeerLiveness.STALE
        assert by_name["charlie"] == PeerLiveness.DEAD

    def test_scan_excludes_self(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "opus", age_seconds=5)
        _write_heartbeat(hb_dir, "lumina", age_seconds=10)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        names = [r.name for r in results]
        assert "opus" not in names
        assert "lumina" in names

    def test_scan_sorted_by_name(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        for name in ["charlie", "alice", "bob"]:
            _write_heartbeat(hb_dir, name)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        assert [r.name for r in results] == ["alice", "bob", "charlie"]

    def test_scan_empty_directory(self, tmp_path):
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        assert monitor.scan() == []

    def test_scan_skips_dotfiles(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir(parents=True)
        _write_heartbeat(hb_dir, "legit")
        (hb_dir / f".tmp{HEARTBEAT_SUFFIX}").write_text("{}")

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        assert len(results) == 1
        assert results[0].name == "legit"

    def test_scan_handles_corrupt_file(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir(parents=True)
        _write_heartbeat(hb_dir, "good")
        (hb_dir / f"bad{HEARTBEAT_SUFFIX}").write_text("not valid json")

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        assert len(results) == 2
        by_name = {r.name: r.status for r in results}
        assert by_name["good"] == PeerLiveness.ALIVE
        assert by_name["bad"] == PeerLiveness.UNKNOWN


# ═══════════════════════════════════════════════════════════
# Convenience methods
# ═══════════════════════════════════════════════════════════


class TestConvenienceMethods:
    """Test alive_peers, dead_peers, all_statuses."""

    def test_alive_peers(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "alice", age_seconds=10)
        _write_heartbeat(hb_dir, "bob", age_seconds=600)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        assert monitor.alive_peers() == ["alice"]

    def test_dead_peers(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "alice", age_seconds=10)
        _write_heartbeat(hb_dir, "bob", age_seconds=600)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        assert monitor.dead_peers() == ["bob"]

    def test_all_statuses_includes_self(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "opus", age_seconds=5)
        _write_heartbeat(hb_dir, "lumina", age_seconds=10)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.all_statuses(include_self=True)
        names = [r.name for r in results]
        assert "opus" in names
        assert "lumina" in names

    def test_all_statuses_excludes_self_by_default(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_heartbeat(hb_dir, "opus", age_seconds=5)
        _write_heartbeat(hb_dir, "lumina", age_seconds=10)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.all_statuses(include_self=False)
        names = [r.name for r in results]
        assert "opus" not in names


# ═══════════════════════════════════════════════════════════
# End-to-end: emit then scan
# ═══════════════════════════════════════════════════════════


class TestEmitAndScan:
    """Test the full emit-then-scan cycle."""

    def test_emit_then_scan(self, tmp_path):
        """Agent A emits, Agent B scans and sees A alive."""
        agent_a = HeartbeatMonitor("opus", comms_root=tmp_path, transports=["syncthing"])
        agent_b = HeartbeatMonitor("lumina", comms_root=tmp_path)

        agent_a.emit()
        results = agent_b.scan()

        assert len(results) == 1
        assert results[0].name == "opus"
        assert results[0].status == PeerLiveness.ALIVE
        assert results[0].transports == ["syncthing"]

    def test_bidirectional_heartbeat(self, tmp_path):
        """Both agents emit, both can see each other."""
        a = HeartbeatMonitor("opus", comms_root=tmp_path)
        b = HeartbeatMonitor("lumina", comms_root=tmp_path)

        a.emit()
        b.emit()

        a_sees = a.scan()
        b_sees = b.scan()

        assert len(a_sees) == 1 and a_sees[0].name == "lumina"
        assert len(b_sees) == 1 and b_sees[0].name == "opus"
