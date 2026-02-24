"""Tests for SKComm delivery acknowledgment tracker."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcomm.ack import (
    ACK_SUFFIX,
    AckStatus,
    AckTracker,
    PendingAck,
    make_ack_envelope,
    should_ack,
)
from skcomm.models import (
    MessageEnvelope,
    MessageMetadata,
    MessagePayload,
    MessageType,
    RoutingConfig,
    Urgency,
)


def _make_envelope(
    sender: str = "opus",
    recipient: str = "lumina",
    content: str = "hello",
    ack_requested: bool = True,
    envelope_id: str | None = None,
) -> MessageEnvelope:
    """Create a test envelope."""
    env = MessageEnvelope(
        sender=sender,
        recipient=recipient,
        payload=MessagePayload(content=content),
        routing=RoutingConfig(ack_requested=ack_requested),
    )
    if envelope_id:
        env = env.model_copy(update={"envelope_id": envelope_id})
    return env


# ═══════════════════════════════════════════════════════════
# PendingAck model
# ═══════════════════════════════════════════════════════════


class TestPendingAck:
    """Test the PendingAck model."""

    def test_basic_creation(self):
        p = PendingAck(envelope_id="e-001", recipient="lumina")
        assert p.status == AckStatus.PENDING
        assert p.confirmed_at is None

    def test_not_expired_initially(self):
        p = PendingAck(envelope_id="e-002", recipient="x", ack_timeout=300)
        assert p.is_expired is False

    def test_expired_after_timeout(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=400)
        p = PendingAck(envelope_id="e-003", recipient="x", sent_at=old, ack_timeout=300)
        assert p.is_expired is True

    def test_confirmed_not_expired(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=400)
        p = PendingAck(
            envelope_id="e-004", recipient="x", sent_at=old,
            ack_timeout=300, status=AckStatus.CONFIRMED,
        )
        assert p.is_expired is False


# ═══════════════════════════════════════════════════════════
# AckTracker — track and resolve
# ═══════════════════════════════════════════════════════════


class TestAckTrackerBasics:
    """Test AckTracker tracking and resolution."""

    def test_track_creates_file(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="track-001")
        pending = tracker.track(env)

        assert pending is not None
        assert pending.envelope_id == "track-001"
        assert (tmp_path / "acks" / f"track-001{ACK_SUFFIX}").exists()

    def test_track_skips_no_ack_requested(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(ack_requested=False, envelope_id="no-ack")
        assert tracker.track(env) is None

    def test_track_skips_ack_envelopes(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="ack-msg")
        ack = env.make_ack("lumina")
        assert tracker.track(ack) is None

    def test_process_ack_confirms(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="confirm-001")
        tracker.track(env)

        ack = env.make_ack("lumina")
        result = tracker.process_ack(ack)

        assert result is not None
        assert result.status == AckStatus.CONFIRMED
        assert result.confirmed_at is not None

    def test_process_ack_unknown_envelope(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="unknown-001")
        ack = env.make_ack("lumina")
        assert tracker.process_ack(ack) is None

    def test_process_non_ack_returns_none(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope()
        assert tracker.process_ack(env) is None

    def test_get_returns_pending(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="get-001")
        tracker.track(env)

        result = tracker.get("get-001")
        assert result is not None
        assert result.status == AckStatus.PENDING

    def test_get_nonexistent(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        assert tracker.get("nope") is None

    def test_remove(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="rm-001")
        tracker.track(env)
        assert tracker.remove("rm-001") is True
        assert tracker.get("rm-001") is None

    def test_remove_nonexistent(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        assert tracker.remove("ghost") is False


# ═══════════════════════════════════════════════════════════
# AckTracker — listing and timeouts
# ═══════════════════════════════════════════════════════════


class TestAckTrackerListing:
    """Test listing and timeout detection."""

    def test_list_pending(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        for i in range(3):
            tracker.track(_make_envelope(envelope_id=f"lp-{i:03d}"))
        assert len(tracker.list_pending()) == 3

    def test_list_confirmed(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="lc-001")
        tracker.track(env)
        tracker.process_ack(env.make_ack("lumina"))
        assert len(tracker.list_confirmed()) == 1

    def test_pending_count(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        tracker.track(_make_envelope(envelope_id="pc-001"))
        tracker.track(_make_envelope(envelope_id="pc-002"))
        assert tracker.pending_count == 2

    def test_check_timeouts(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks", default_timeout=0)
        tracker.track(_make_envelope(envelope_id="to-001"))
        time.sleep(0.05)

        timed_out = tracker.check_timeouts()
        assert len(timed_out) == 1
        assert timed_out[0].status == AckStatus.TIMED_OUT

        loaded = tracker.get("to-001")
        assert loaded.status == AckStatus.TIMED_OUT

    def test_list_timed_out(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks", default_timeout=0)
        tracker.track(_make_envelope(envelope_id="lto-001"))
        time.sleep(0.05)

        assert len(tracker.list_timed_out()) == 1

    def test_purge_confirmed(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="purge-001")
        tracker.track(env)
        tracker.process_ack(env.make_ack("lumina"))

        removed = tracker.purge_confirmed(max_age=0)
        assert removed == 1
        assert tracker.get("purge-001") is None

    def test_purge_keeps_recent(self, tmp_path):
        tracker = AckTracker(acks_dir=tmp_path / "acks")
        env = _make_envelope(envelope_id="keep-001")
        tracker.track(env)
        tracker.process_ack(env.make_ack("lumina"))

        removed = tracker.purge_confirmed(max_age=86400)
        assert removed == 0


# ═══════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════


class TestHelpers:
    """Test should_ack and make_ack_envelope helpers."""

    def test_should_ack_normal_message(self):
        env = _make_envelope(ack_requested=True)
        assert should_ack(env) is True

    def test_should_not_ack_when_not_requested(self):
        env = _make_envelope(ack_requested=False)
        assert should_ack(env) is False

    def test_should_not_ack_an_ack(self):
        env = _make_envelope()
        ack = env.make_ack("lumina")
        assert should_ack(ack) is False

    def test_make_ack_envelope(self):
        env = _make_envelope(envelope_id="ack-test-001")
        ack = make_ack_envelope(env, "lumina")
        assert ack.is_ack
        assert ack.payload.content == "ack-test-001"
        assert ack.sender == "lumina"
        assert ack.recipient == "opus"
        assert ack.routing.ack_requested is False


# ═══════════════════════════════════════════════════════════
# End-to-end: track -> ack -> confirm
# ═══════════════════════════════════════════════════════════


class TestEndToEnd:
    """Test the full ACK lifecycle."""

    def test_full_lifecycle(self, tmp_path):
        """Track a message, receive its ACK, confirm delivery."""
        tracker = AckTracker(acks_dir=tmp_path / "acks")

        sent = _make_envelope(sender="opus", recipient="lumina", envelope_id="e2e-001")
        tracker.track(sent)
        assert tracker.pending_count == 1

        ack = sent.make_ack("lumina")
        tracker.process_ack(ack)
        assert tracker.pending_count == 0
        assert len(tracker.list_confirmed()) == 1

        confirmed = tracker.get("e2e-001")
        assert confirmed.status == AckStatus.CONFIRMED

    def test_multiple_messages(self, tmp_path):
        """Track multiple messages, confirm some, timeout others."""
        tracker = AckTracker(acks_dir=tmp_path / "acks", default_timeout=0)

        env1 = _make_envelope(envelope_id="multi-001")
        env2 = _make_envelope(envelope_id="multi-002")
        tracker.track(env1)
        tracker.track(env2)

        tracker.process_ack(env1.make_ack("lumina"))
        time.sleep(0.05)
        tracker.check_timeouts()

        assert tracker.get("multi-001").status == AckStatus.CONFIRMED
        assert tracker.get("multi-002").status == AckStatus.TIMED_OUT
