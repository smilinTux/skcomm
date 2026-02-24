"""Tests for SKComm transport metrics collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcomm.metrics import MetricsCollector, TransportStats


# ═══════════════════════════════════════════════════════════
# TransportStats model
# ═══════════════════════════════════════════════════════════


class TestTransportStats:
    """Test the TransportStats model."""

    def test_defaults(self):
        s = TransportStats(transport="syncthing")
        assert s.sends_ok == 0
        assert s.total_sends == 0
        assert s.success_rate == 0.0
        assert s.avg_latency_ms == 0.0

    def test_success_rate(self):
        s = TransportStats(transport="x", sends_ok=8, sends_fail=2)
        assert s.success_rate == 80.0

    def test_avg_latency(self):
        s = TransportStats(transport="x", sends_ok=4, total_latency_ms=100.0)
        assert s.avg_latency_ms == 25.0

    def test_total_sends(self):
        s = TransportStats(transport="x", sends_ok=5, sends_fail=3)
        assert s.total_sends == 8


# ═══════════════════════════════════════════════════════════
# MetricsCollector — record operations
# ═══════════════════════════════════════════════════════════


class TestRecording:
    """Test recording send and receive events."""

    def test_record_successful_send(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("syncthing", success=True, latency_ms=15.0)

        s = mc.get("syncthing")
        assert s.sends_ok == 1
        assert s.sends_fail == 0
        assert s.total_latency_ms == 15.0
        assert s.min_latency_ms == 15.0
        assert s.max_latency_ms == 15.0

    def test_record_failed_send(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("nostr", success=False, error="relay timeout")

        s = mc.get("nostr")
        assert s.sends_ok == 0
        assert s.sends_fail == 1
        assert s.last_error == "relay timeout"
        assert len(s.recent_errors) == 1

    def test_record_multiple_sends(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("file", success=True, latency_ms=5.0)
        mc.record_send("file", success=True, latency_ms=15.0)
        mc.record_send("file", success=False, error="disk full")

        s = mc.get("file")
        assert s.sends_ok == 2
        assert s.sends_fail == 1
        assert s.min_latency_ms == 5.0
        assert s.max_latency_ms == 15.0
        assert s.avg_latency_ms == 10.0

    def test_record_receive(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_receive("syncthing", count=3)

        s = mc.get("syncthing")
        assert s.receives == 3
        assert s.last_receive is not None

    def test_record_multiple_transports(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("syncthing", success=True, latency_ms=10.0)
        mc.record_send("nostr", success=True, latency_ms=200.0)
        mc.record_send("file", success=False, error="not found")

        assert len(mc.all_stats()) == 3

    def test_get_nonexistent(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        assert mc.get("ghost") is None


# ═══════════════════════════════════════════════════════════
# MetricsCollector — persistence
# ═══════════════════════════════════════════════════════════


class TestPersistence:
    """Test metrics persistence to disk."""

    def test_saves_to_file(self, tmp_path):
        path = tmp_path / "m.json"
        mc = MetricsCollector(metrics_path=path)
        mc.record_send("syncthing", success=True, latency_ms=10.0)
        assert path.exists()

        data = json.loads(path.read_text())
        assert "syncthing" in data

    def test_loads_from_file(self, tmp_path):
        path = tmp_path / "m.json"
        mc1 = MetricsCollector(metrics_path=path)
        mc1.record_send("syncthing", success=True, latency_ms=10.0)
        mc1.record_send("syncthing", success=True, latency_ms=20.0)

        mc2 = MetricsCollector(metrics_path=path)
        s = mc2.get("syncthing")
        assert s.sends_ok == 2
        assert s.total_latency_ms == 30.0

    def test_survives_missing_file(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "nope.json")
        assert mc.all_stats() == []

    def test_survives_corrupt_file(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text("not json")
        mc = MetricsCollector(metrics_path=path)
        assert mc.all_stats() == []

    def test_atomic_write(self, tmp_path):
        path = tmp_path / "m.json"
        mc = MetricsCollector(metrics_path=path)
        mc.record_send("x", success=True)
        tmp_files = list(tmp_path.glob(".*"))
        assert len(tmp_files) == 0


# ═══════════════════════════════════════════════════════════
# MetricsCollector — summary and reset
# ═══════════════════════════════════════════════════════════


class TestSummaryAndReset:
    """Test summary generation and reset."""

    def test_summary(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("syncthing", success=True, latency_ms=10.0)
        mc.record_send("nostr", success=False, error="fail")
        mc.record_receive("syncthing", count=5)

        s = mc.summary()
        assert s["total_sends_ok"] == 1
        assert s["total_sends_fail"] == 1
        assert s["total_receives"] == 5
        assert "50.0%" in s["overall_success_rate"]
        assert "syncthing" in s["transports"]
        assert "nostr" in s["transports"]

    def test_summary_empty(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        s = mc.summary()
        assert s["total_sends_ok"] == 0
        assert s["overall_success_rate"] == "N/A"

    def test_reset_single(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("syncthing", success=True)
        mc.record_send("nostr", success=True)
        mc.reset("syncthing")
        assert mc.get("syncthing") is None
        assert mc.get("nostr") is not None

    def test_reset_all(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("syncthing", success=True)
        mc.record_send("nostr", success=True)
        mc.reset()
        assert mc.all_stats() == []

    def test_error_history_capped(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        for i in range(30):
            mc.record_send("nostr", success=False, error=f"error-{i}")
        s = mc.get("nostr")
        assert len(s.recent_errors) == 20

    def test_all_stats_sorted(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "m.json")
        mc.record_send("zzz", success=True)
        mc.record_send("aaa", success=True)
        mc.record_send("mmm", success=True)
        names = [s.transport for s in mc.all_stats()]
        assert names == ["aaa", "mmm", "zzz"]
