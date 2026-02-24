"""Tests for SKComm persistent message queue — retry, expiry, drain."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcomm.queue import (
    ENVELOPE_SUFFIX,
    META_SUFFIX,
    MessageQueue,
    QueueMeta,
    QueuedEnvelope,
)


def _make_envelope(envelope_id: str = "test-001", recipient: str = "lumina") -> bytes:
    """Create minimal envelope bytes for testing."""
    return json.dumps({
        "skcomm_version": "1.0.0",
        "envelope_id": envelope_id,
        "sender": "opus",
        "recipient": recipient,
        "payload": {"content": "hello", "content_type": "text"},
    }).encode()


# ═══════════════════════════════════════════════════════════
# QueueMeta model
# ═══════════════════════════════════════════════════════════


class TestQueueMeta:
    """Test the QueueMeta model."""

    def test_basic_creation(self):
        m = QueueMeta(envelope_id="e-001", recipient="lumina")
        assert m.envelope_id == "e-001"
        assert m.attempts == 0
        assert m.is_expired is False

    def test_is_expired_by_ttl(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=100)
        m = QueueMeta(envelope_id="e-002", recipient="x", queued_at=old, ttl=60)
        assert m.is_expired is True

    def test_is_not_expired(self):
        m = QueueMeta(envelope_id="e-003", recipient="x", ttl=86400)
        assert m.is_expired is False

    def test_is_ready_initially(self):
        m = QueueMeta(envelope_id="e-004", recipient="x")
        assert m.is_ready is True

    def test_is_ready_respects_next_retry(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        m = QueueMeta(envelope_id="e-005", recipient="x", next_retry=future)
        assert m.is_ready is False

    def test_is_ready_after_retry_time(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        m = QueueMeta(envelope_id="e-006", recipient="x", next_retry=past)
        assert m.is_ready is True

    def test_expired_is_not_ready(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=200)
        m = QueueMeta(envelope_id="e-007", recipient="x", queued_at=old, ttl=60)
        assert m.is_ready is False

    def test_record_attempt_increments_counter(self):
        m = QueueMeta(envelope_id="e-008", recipient="x")
        m.record_attempt(error="fail")
        assert m.attempts == 1
        assert m.error == "fail"
        assert m.last_attempt is not None

    def test_record_attempt_backoff(self):
        m = QueueMeta(envelope_id="e-009", recipient="x", backoff=[5, 15, 60])
        m.record_attempt(error="fail")
        assert m.next_retry > datetime.now(timezone.utc)
        expected_delay = 5
        actual_delay = (m.next_retry - m.last_attempt).total_seconds()
        assert abs(actual_delay - expected_delay) < 1

    def test_record_attempt_backoff_escalation(self):
        m = QueueMeta(envelope_id="e-010", recipient="x", backoff=[5, 15, 60])
        m.record_attempt(error="fail")
        m.record_attempt(error="fail again")
        expected_delay = 15
        actual_delay = (m.next_retry - m.last_attempt).total_seconds()
        assert abs(actual_delay - expected_delay) < 1

    def test_record_attempt_success_clears_error(self):
        m = QueueMeta(envelope_id="e-011", recipient="x")
        m.record_attempt(error="fail")
        m.record_attempt(error=None)
        assert m.error is None
        assert m.attempts == 2


# ═══════════════════════════════════════════════════════════
# MessageQueue — enqueue / dequeue / peek
# ═══════════════════════════════════════════════════════════


class TestMessageQueueBasics:
    """Test basic queue operations."""

    def test_enqueue_creates_files(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        env = _make_envelope("enq-001")
        meta = q.enqueue(env, "lumina", envelope_id="enq-001")

        assert meta.envelope_id == "enq-001"
        assert (tmp_path / "queue" / f"enq-001{ENVELOPE_SUFFIX}").exists()
        assert (tmp_path / "queue" / f"enq-001{META_SUFFIX}").exists()

    def test_enqueue_extracts_id_from_bytes(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        env = _make_envelope("auto-id-001")
        meta = q.enqueue(env, "lumina")
        assert meta.envelope_id == "auto-id-001"

    def test_dequeue_removes_files(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("del-001"), "lumina", envelope_id="del-001")
        assert q.dequeue("del-001") is True
        assert not (tmp_path / "queue" / f"del-001{ENVELOPE_SUFFIX}").exists()
        assert not (tmp_path / "queue" / f"del-001{META_SUFFIX}").exists()

    def test_dequeue_nonexistent(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        assert q.dequeue("ghost") is False

    def test_peek_returns_envelope(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        env = _make_envelope("peek-001")
        q.enqueue(env, "lumina", envelope_id="peek-001")

        queued = q.peek("peek-001")
        assert queued is not None
        assert queued.envelope_bytes == env
        assert queued.meta.envelope_id == "peek-001"
        assert queued.meta.recipient == "lumina"

    def test_peek_nonexistent(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        assert q.peek("nope") is None

    def test_size(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        assert q.size == 0
        q.enqueue(_make_envelope("s-001"), "a", envelope_id="s-001")
        q.enqueue(_make_envelope("s-002"), "b", envelope_id="s-002")
        assert q.size == 2

    def test_atomic_write_no_tmp_files(self, tmp_path):
        """No .tmp files should remain after enqueue."""
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("atomic-001"), "lumina", envelope_id="atomic-001")
        tmp_files = list((tmp_path / "queue").glob(".*"))
        assert len(tmp_files) == 0


# ═══════════════════════════════════════════════════════════
# MessageQueue — list operations
# ═══════════════════════════════════════════════════════════


class TestMessageQueueListing:
    """Test list_all and list_pending."""

    def test_list_all(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("l-001"), "a", envelope_id="l-001")
        q.enqueue(_make_envelope("l-002"), "b", envelope_id="l-002")
        q.enqueue(_make_envelope("l-003"), "c", envelope_id="l-003")
        assert len(q.list_all()) == 3

    def test_list_all_empty(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        assert q.list_all() == []

    def test_list_pending_skips_not_ready(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("p-001"), "a", envelope_id="p-001")

        queued = q.peek("p-001")
        queued.meta.next_retry = datetime.now(timezone.utc) + timedelta(hours=1)
        q.update_meta(queued.meta)

        pending = q.list_pending()
        assert len(pending) == 0

    def test_list_pending_includes_ready(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("r-001"), "a", envelope_id="r-001")
        pending = q.list_pending()
        assert len(pending) == 1
        assert pending[0].envelope_id == "r-001"


# ═══════════════════════════════════════════════════════════
# MessageQueue — expiry
# ═══════════════════════════════════════════════════════════


class TestMessageQueueExpiry:
    """Test TTL-based expiry and purge."""

    def test_purge_removes_expired(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("exp-001"), "a", envelope_id="exp-001", ttl=0)
        time.sleep(0.05)

        removed = q.purge_expired()
        assert removed == 1
        assert q.size == 0

    def test_purge_keeps_fresh(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("fresh-001"), "a", envelope_id="fresh-001", ttl=86400)
        removed = q.purge_expired()
        assert removed == 0
        assert q.size == 1

    def test_enqueue_with_custom_ttl(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        meta = q.enqueue(_make_envelope("ttl-001"), "a", envelope_id="ttl-001", ttl=3600)
        assert meta.ttl == 3600


# ═══════════════════════════════════════════════════════════
# MessageQueue — drain
# ═══════════════════════════════════════════════════════════


class TestMessageQueueDrain:
    """Test the drain loop."""

    def test_drain_delivers_and_dequeues(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("d-001"), "a", envelope_id="d-001")
        q.enqueue(_make_envelope("d-002"), "b", envelope_id="d-002")

        delivered, failed = q.drain(lambda env, recip: True)
        assert delivered == 2
        assert failed == 0
        assert q.size == 0

    def test_drain_handles_failure(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("f-001"), "a", envelope_id="f-001")

        delivered, failed = q.drain(lambda env, recip: False)
        assert delivered == 0
        assert failed == 1
        assert q.size == 1

        queued = q.peek("f-001")
        assert queued.meta.attempts == 1
        assert queued.meta.error == "Transport delivery failed"

    def test_drain_handles_exception(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("x-001"), "a", envelope_id="x-001")

        def explode(env, recip):
            raise ConnectionError("boom")

        delivered, failed = q.drain(explode)
        assert delivered == 0
        assert failed == 1
        queued = q.peek("x-001")
        assert "boom" in queued.meta.error

    def test_drain_purges_expired_first(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("old-001"), "a", envelope_id="old-001", ttl=0)
        time.sleep(0.05)
        q.enqueue(_make_envelope("new-001"), "b", envelope_id="new-001", ttl=86400)

        delivered, failed = q.drain(lambda env, recip: True)
        assert delivered == 1
        assert q.size == 0

    def test_drain_empty_queue(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        delivered, failed = q.drain(lambda env, recip: True)
        assert delivered == 0
        assert failed == 0

    def test_drain_partial_success(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("p-001"), "succeed", envelope_id="p-001")
        q.enqueue(_make_envelope("p-002"), "fail", envelope_id="p-002")

        def selective(env, recip):
            return recip == "succeed"

        delivered, failed = q.drain(selective)
        assert delivered == 1
        assert failed == 1
        assert q.size == 1

    def test_drain_skips_not_ready(self, tmp_path):
        q = MessageQueue(queue_dir=tmp_path / "queue")
        q.enqueue(_make_envelope("nr-001"), "a", envelope_id="nr-001")

        queued = q.peek("nr-001")
        queued.meta.next_retry = datetime.now(timezone.utc) + timedelta(hours=1)
        q.update_meta(queued.meta)

        delivered, failed = q.drain(lambda env, recip: True)
        assert delivered == 0
        assert failed == 0
        assert q.size == 1
