"""Tests for the SKComm heartbeat protocol — v1 alive/dead detection and v2 rich beacons."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcomm.heartbeat import (
    HEARTBEAT_SUFFIX,
    HeartbeatConfig,
    HeartbeatMonitor,
    HeartbeatPayload,
    NodeHeartbeat,
    NodeHeartbeatMonitor,
    NodeResources,
    HeartbeatPublisher,
    PeerHeartbeat,
    PeerLiveness,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_v1_heartbeat(
    hb_dir: Path,
    agent: str,
    age_seconds: float = 0,
    transports: list[str] | None = None,
    fingerprint: str | None = None,
) -> Path:
    """Write a v1 heartbeat file with a configurable age."""
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


def _write_v2_heartbeat(
    hb_dir: Path,
    node_id: str,
    age_seconds: float = 0,
    ttl_seconds: int = 120,
    capabilities: list[str] | None = None,
    state: str = "active",
) -> Path:
    """Write a v2 heartbeat file with a configurable age."""
    hb_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    hb = NodeHeartbeat(
        node_id=node_id,
        timestamp=ts,
        ttl_seconds=ttl_seconds,
        state=state,
        capabilities=capabilities or [],
    )
    path = hb_dir / f"{node_id}.json"
    path.write_text(hb.model_dump_json(indent=2))
    return path


# ═══════════════════════════════════════════════════════════
# v1: HeartbeatPayload model
# ═══════════════════════════════════════════════════════════


class TestHeartbeatPayload:
    """Test the v1 heartbeat data model."""

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
# v1: HeartbeatMonitor — emit
# ═══════════════════════════════════════════════════════════


class TestHeartbeatEmit:
    """Test v1 heartbeat emission."""

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
# v1: HeartbeatMonitor — read and classify
# ═══════════════════════════════════════════════════════════


class TestHeartbeatClassification:
    """Test v1 peer liveness classification."""

    def test_alive_peer(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "lumina", age_seconds=30)
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)

        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.ALIVE
        assert status.age_seconds is not None
        assert status.age_seconds < 35

    def test_stale_peer(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "lumina", age_seconds=180)
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)

        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.STALE

    def test_dead_peer(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "lumina", age_seconds=600)
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)

        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.DEAD

    def test_unknown_peer(self, tmp_path):
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        status = monitor.peer_status("ghost")
        assert status.status == PeerLiveness.UNKNOWN

    def test_custom_timeouts(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "lumina", age_seconds=50)
        monitor = HeartbeatMonitor(
            "opus", comms_root=tmp_path,
            alive_timeout=30, stale_timeout=60,
        )
        status = monitor.peer_status("lumina")
        assert status.status == PeerLiveness.STALE

    def test_status_includes_transports(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "lumina", transports=["syncthing", "file"])
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        status = monitor.peer_status("lumina")
        assert status.transports == ["syncthing", "file"]

    def test_status_includes_fingerprint(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "lumina", fingerprint="FP_LUMINA")
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        status = monitor.peer_status("lumina")
        assert status.fingerprint == "FP_LUMINA"


# ═══════════════════════════════════════════════════════════
# v1: HeartbeatMonitor — scan
# ═══════════════════════════════════════════════════════════


class TestHeartbeatScan:
    """Test scanning all v1 peer heartbeats."""

    def test_scan_multiple_peers(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "alice", age_seconds=10)
        _write_v1_heartbeat(hb_dir, "bob", age_seconds=200)
        _write_v1_heartbeat(hb_dir, "charlie", age_seconds=600)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()

        assert len(results) == 3
        by_name = {r.name: r.status for r in results}
        assert by_name["alice"] == PeerLiveness.ALIVE
        assert by_name["bob"] == PeerLiveness.STALE
        assert by_name["charlie"] == PeerLiveness.DEAD

    def test_scan_excludes_self(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "opus", age_seconds=5)
        _write_v1_heartbeat(hb_dir, "lumina", age_seconds=10)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        names = [r.name for r in results]
        assert "opus" not in names
        assert "lumina" in names

    def test_scan_sorted_by_name(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        for name in ["charlie", "alice", "bob"]:
            _write_v1_heartbeat(hb_dir, name)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        assert [r.name for r in results] == ["alice", "bob", "charlie"]

    def test_scan_empty_directory(self, tmp_path):
        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        assert monitor.scan() == []

    def test_scan_skips_dotfiles(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir(parents=True)
        _write_v1_heartbeat(hb_dir, "legit")
        (hb_dir / f".tmp{HEARTBEAT_SUFFIX}").write_text("{}")

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        assert len(results) == 1
        assert results[0].name == "legit"

    def test_scan_handles_corrupt_file(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir(parents=True)
        _write_v1_heartbeat(hb_dir, "good")
        (hb_dir / f"bad{HEARTBEAT_SUFFIX}").write_text("not valid json")

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.scan()
        assert len(results) == 2
        by_name = {r.name: r.status for r in results}
        assert by_name["good"] == PeerLiveness.ALIVE
        assert by_name["bad"] == PeerLiveness.UNKNOWN


# ═══════════════════════════════════════════════════════════
# v1: Convenience methods
# ═══════════════════════════════════════════════════════════


class TestConvenienceMethods:
    """Test alive_peers, dead_peers, all_statuses (v1)."""

    def test_alive_peers(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "alice", age_seconds=10)
        _write_v1_heartbeat(hb_dir, "bob", age_seconds=600)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        assert monitor.alive_peers() == ["alice"]

    def test_dead_peers(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "alice", age_seconds=10)
        _write_v1_heartbeat(hb_dir, "bob", age_seconds=600)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        assert monitor.dead_peers() == ["bob"]

    def test_all_statuses_includes_self(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "opus", age_seconds=5)
        _write_v1_heartbeat(hb_dir, "lumina", age_seconds=10)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.all_statuses(include_self=True)
        names = [r.name for r in results]
        assert "opus" in names
        assert "lumina" in names

    def test_all_statuses_excludes_self_by_default(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v1_heartbeat(hb_dir, "opus", age_seconds=5)
        _write_v1_heartbeat(hb_dir, "lumina", age_seconds=10)

        monitor = HeartbeatMonitor("opus", comms_root=tmp_path)
        results = monitor.all_statuses(include_self=False)
        names = [r.name for r in results]
        assert "opus" not in names


# ═══════════════════════════════════════════════════════════
# v1: End-to-end — emit then scan
# ═══════════════════════════════════════════════════════════


class TestEmitAndScan:
    """Test the full v1 emit-then-scan cycle."""

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


# ═══════════════════════════════════════════════════════════
# v2: NodeHeartbeat model
# ═══════════════════════════════════════════════════════════


class TestNodeHeartbeat:
    """Test the v2 NodeHeartbeat model."""

    def test_default_fields(self):
        hb = NodeHeartbeat(node_id="jarvis-desktop")
        assert hb.node_id == "jarvis-desktop"
        assert hb.state == "active"
        assert hb.ttl_seconds == 120
        assert hb.version == "0.1.0"
        assert hb.timestamp is not None

    def test_full_fields(self):
        hb = NodeHeartbeat(
            node_id="jarvis-desktop",
            state="busy",
            agent_name="jarvis",
            capabilities=["code", "gpu"],
            resources=NodeResources(cpu_percent=45.2, gpu_available=True),
            claimed_tasks=["17d8d71f"],
            loaded_models=["deepseek-v3.2"],
            skcomm_status="online",
        )
        assert hb.agent_name == "jarvis"
        assert "gpu" in hb.capabilities
        assert hb.resources.gpu_available is True
        assert hb.claimed_tasks == ["17d8d71f"]

    def test_serialization_roundtrip(self):
        hb = NodeHeartbeat(
            node_id="lumina-laptop",
            capabilities=["code", "skchat"],
            resources=NodeResources(ram_total_gb=16.0, ram_used_gb=8.0),
        )
        data = hb.model_dump_json(indent=2)
        loaded = NodeHeartbeat.model_validate_json(data)
        assert loaded.node_id == "lumina-laptop"
        assert loaded.capabilities == ["code", "skchat"]
        assert loaded.resources.ram_total_gb == 16.0

    def test_not_expired_when_fresh(self):
        hb = NodeHeartbeat(node_id="jarvis-desktop", ttl_seconds=120)
        assert hb.is_expired() is False

    def test_expired_when_old(self):
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=200)
        hb = NodeHeartbeat(node_id="jarvis-desktop", timestamp=old_ts, ttl_seconds=120)
        assert hb.is_expired() is True

    def test_expired_exactly_at_ttl_boundary(self):
        # Provide an explicit reference time so clock drift cannot affect the result.
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc)  # exactly 120s later
        hb = NodeHeartbeat(node_id="n1", timestamp=ts, ttl_seconds=120)
        # age == ttl_seconds exactly → not yet over the limit (age > ttl, not >=)
        assert hb.is_expired(now=now) is False

    def test_expired_past_ttl_boundary(self):
        ts = datetime.now(timezone.utc) - timedelta(seconds=121)
        hb = NodeHeartbeat(node_id="n1", timestamp=ts, ttl_seconds=120)
        assert hb.is_expired() is True


# ═══════════════════════════════════════════════════════════
# v2: HeartbeatPublisher — publish/read roundtrip
# ═══════════════════════════════════════════════════════════


class TestHeartbeatPublisher:
    """Test the v2 HeartbeatPublisher."""

    def test_publish_creates_file(self, tmp_path):
        cfg = HeartbeatConfig(node_id="jarvis-desktop", sync_root=tmp_path)
        pub = HeartbeatPublisher(config=cfg)
        path = pub.publish()
        assert path.exists()
        assert path.name == "jarvis-desktop.json"

    def test_publish_file_is_valid_json(self, tmp_path):
        cfg = HeartbeatConfig(node_id="jarvis-desktop", sync_root=tmp_path)
        pub = HeartbeatPublisher(config=cfg)
        path = pub.publish()
        data = json.loads(path.read_text())
        assert data["node_id"] == "jarvis-desktop"

    def test_publish_roundtrip_via_model(self, tmp_path):
        cfg = HeartbeatConfig(
            node_id="jarvis-desktop",
            agent_name="jarvis",
            capabilities=["code", "gpu"],
            ttl_seconds=60,
            sync_root=tmp_path,
        )
        pub = HeartbeatPublisher(config=cfg)
        path = pub.publish()
        loaded = NodeHeartbeat.model_validate_json(path.read_text())
        assert loaded.node_id == "jarvis-desktop"
        assert loaded.agent_name == "jarvis"
        assert loaded.ttl_seconds == 60
        assert "code" in loaded.capabilities
        assert "gpu" in loaded.capabilities

    def test_publish_atomic_no_tmp_files(self, tmp_path):
        """No .tmp files should remain after publish."""
        cfg = HeartbeatConfig(node_id="jarvis-desktop", sync_root=tmp_path)
        pub = HeartbeatPublisher(config=cfg)
        pub.publish()
        hb_dir = tmp_path / "heartbeats"
        assert len(list(hb_dir.glob(".*"))) == 0

    def test_publish_overwrites_previous(self, tmp_path):
        cfg = HeartbeatConfig(node_id="jarvis-desktop", sync_root=tmp_path)
        pub = HeartbeatPublisher(config=cfg)
        pub.publish()
        time.sleep(0.05)
        path = pub.publish()
        loaded = NodeHeartbeat.model_validate_json(path.read_text())
        age = (datetime.now(timezone.utc) - loaded.timestamp).total_seconds()
        assert age < 5

    def test_publisher_path_properties(self, tmp_path):
        cfg = HeartbeatConfig(node_id="mynode", sync_root=tmp_path)
        pub = HeartbeatPublisher(config=cfg)
        assert pub.heartbeat_path == pub.heartbeat_dir / "mynode.json"

    def test_start_stop_background_thread(self, tmp_path):
        """start() should produce a heartbeat file and stop() should terminate the thread."""
        cfg = HeartbeatConfig(
            node_id="jarvis-desktop",
            sync_root=tmp_path,
            publish_interval_seconds=1,
        )
        pub = HeartbeatPublisher(config=cfg)
        pub.start()
        time.sleep(1.5)
        pub.stop()
        assert pub.heartbeat_path.exists()

    def test_start_idempotent(self, tmp_path):
        """Calling start() twice should not create a second thread."""
        cfg = HeartbeatConfig(
            node_id="jarvis-desktop",
            sync_root=tmp_path,
            publish_interval_seconds=60,
        )
        pub = HeartbeatPublisher(config=cfg)
        pub.start()
        t1 = pub._thread
        pub.start()  # should be a no-op
        assert pub._thread is t1
        pub.stop()


# ═══════════════════════════════════════════════════════════
# v2: NodeHeartbeatMonitor
# ═══════════════════════════════════════════════════════════


class TestNodeHeartbeatMonitor:
    """Test the v2 NodeHeartbeatMonitor."""

    def test_empty_directory(self, tmp_path):
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        assert monitor.discover_nodes() == []
        assert monitor.stale_nodes() == []

    def test_get_node_missing(self, tmp_path):
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        assert monitor.get_node("ghost") is None

    def test_get_node_returns_correct_entry(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "jarvis-desktop")
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        hb = monitor.get_node("jarvis-desktop")
        assert hb is not None
        assert hb.node_id == "jarvis-desktop"

    def test_discover_nodes_live_only(self, tmp_path):
        """discover_nodes returns only non-expired entries."""
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "live-node", age_seconds=10, ttl_seconds=120)
        _write_v2_heartbeat(hb_dir, "dead-node", age_seconds=200, ttl_seconds=120)
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        nodes = monitor.discover_nodes()
        ids = [n.node_id for n in nodes]
        assert "live-node" in ids
        assert "dead-node" not in ids

    def test_stale_nodes_returns_expired(self, tmp_path):
        """stale_nodes returns only expired entries."""
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "live-node", age_seconds=10, ttl_seconds=120)
        _write_v2_heartbeat(hb_dir, "dead-node", age_seconds=200, ttl_seconds=120)
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        stale = monitor.stale_nodes()
        ids = [n.node_id for n in stale]
        assert "dead-node" in ids
        assert "live-node" not in ids

    def test_find_capable_returns_matching_live_nodes(self, tmp_path):
        """find_capable filters live nodes by capability (case-insensitive)."""
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "gpu-node", capabilities=["code", "GPU"])
        _write_v2_heartbeat(hb_dir, "cpu-node", capabilities=["code"])
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        gpu_nodes = monitor.find_capable("gpu")
        ids = [n.node_id for n in gpu_nodes]
        assert "gpu-node" in ids
        assert "cpu-node" not in ids

    def test_find_capable_excludes_stale_nodes(self, tmp_path):
        """find_capable should not return nodes past their TTL."""
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "old-gpu", age_seconds=300, ttl_seconds=60, capabilities=["gpu"])
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        result = monitor.find_capable("gpu")
        assert result == []

    def test_all_nodes_includes_expired(self, tmp_path):
        """all_nodes returns everything regardless of TTL."""
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "live-node", age_seconds=10, ttl_seconds=120)
        _write_v2_heartbeat(hb_dir, "dead-node", age_seconds=300, ttl_seconds=60)
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        all_n = monitor.all_nodes()
        ids = {n.node_id for n in all_n}
        assert "live-node" in ids
        assert "dead-node" in ids

    def test_skips_dotfiles(self, tmp_path):
        """Hidden / tmp files in the heartbeat dir are ignored."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir(parents=True)
        _write_v2_heartbeat(hb_dir, "real-node")
        (hb_dir / ".real-node.json.tmp").write_text("{}")
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        nodes = monitor.all_nodes()
        assert len(nodes) == 1
        assert nodes[0].node_id == "real-node"

    def test_handles_corrupt_file_gracefully(self, tmp_path):
        """Invalid JSON files are silently skipped."""
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "good-node")
        (hb_dir / "bad-node.json").write_text("not json")
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        nodes = monitor.all_nodes()
        assert len(nodes) == 1
        assert nodes[0].node_id == "good-node"

    def test_publish_then_discover_roundtrip(self, tmp_path):
        """Publisher writes a file that Monitor can discover as a live node."""
        cfg = HeartbeatConfig(
            node_id="jarvis-desktop",
            agent_name="jarvis",
            capabilities=["code", "gpu"],
            ttl_seconds=120,
            sync_root=tmp_path,
        )
        pub = HeartbeatPublisher(config=cfg)
        pub.publish()

        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        nodes = monitor.discover_nodes()
        assert len(nodes) == 1
        assert nodes[0].node_id == "jarvis-desktop"
        assert "gpu" in nodes[0].capabilities

    def test_capability_filter_case_insensitive(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        _write_v2_heartbeat(hb_dir, "node-a", capabilities=["GPU", "Code"])
        monitor = NodeHeartbeatMonitor(sync_root=tmp_path)
        assert len(monitor.find_capable("gpu")) == 1
        assert len(monitor.find_capable("GPU")) == 1
        assert len(monitor.find_capable("code")) == 1
