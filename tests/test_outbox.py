"""Tests for the SKComm persistent outbox with retry and dead letter."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skcomm.outbox import OutboxEntry, PersistentOutbox


@pytest.fixture()
def outbox(tmp_path: Path) -> PersistentOutbox:
    """Fresh outbox in a temp directory."""
    return PersistentOutbox(
        outbox_dir=tmp_path / "outbox",
        max_retries=3,
        base_backoff=1,
    )


def _make_envelope_json(envelope_id: str = "test-env-001") -> str:
    """Create a minimal envelope JSON string."""
    return json.dumps({
        "skcomm_version": "1.0.0",
        "envelope_id": envelope_id,
        "sender": "alice",
        "recipient": "bob",
        "payload": {"content": "Hello!", "content_type": "text"},
        "routing": {"mode": "failover"},
        "metadata": {},
    })


class TestEnqueue:
    """Tests for adding messages to the outbox."""

    def test_enqueue_creates_file(self, outbox: PersistentOutbox) -> None:
        """Happy path: enqueue creates a pending file."""
        entry = outbox.enqueue("env-001", "bob", _make_envelope_json("env-001"), "timeout")

        assert entry.envelope_id == "env-001"
        assert entry.attempt_count == 1
        assert entry.last_error == "timeout"
        assert outbox.pending_count == 1

    def test_enqueue_multiple(self, outbox: PersistentOutbox) -> None:
        """Multiple messages can be queued."""
        for i in range(5):
            outbox.enqueue(f"env-{i}", "bob", _make_envelope_json(f"env-{i}"))
        assert outbox.pending_count == 5

    def test_enqueue_sets_next_retry(self, outbox: PersistentOutbox) -> None:
        """Queued entry has a future retry time."""
        entry = outbox.enqueue("env-001", "bob", _make_envelope_json())
        assert entry.next_retry_at is not None
        assert entry.next_retry_at > datetime.now(timezone.utc)


class TestRetryAll:
    """Tests for the retry sweep."""

    def test_retry_with_no_router(self, outbox: PersistentOutbox) -> None:
        """Retry without router doesn't deliver but doesn't crash."""
        outbox.enqueue("env-001", "bob", _make_envelope_json())

        # Reason: force next_retry to past so it's eligible
        entry_path = outbox._pending / "env-001.json"
        entry = OutboxEntry.model_validate_json(entry_path.read_text())
        entry.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        entry_path.write_text(entry.model_dump_json(indent=2))

        result = outbox.retry_all()
        assert result["retried"] == 1
        assert result["delivered"] == 0

    def test_retry_skips_future(self, outbox: PersistentOutbox) -> None:
        """Messages with future retry time are skipped."""
        outbox.enqueue("env-001", "bob", _make_envelope_json())
        result = outbox.retry_all()
        assert result["skipped"] == 1
        assert result["retried"] == 0

    def test_retry_delivers_with_router(self, outbox: PersistentOutbox) -> None:
        """Successful retry removes the message from pending."""
        mock_router = MagicMock()
        mock_report = MagicMock()
        mock_report.delivered = True
        mock_router.route.return_value = mock_report
        outbox._router = mock_router

        outbox.enqueue("env-001", "bob", _make_envelope_json())

        entry_path = outbox._pending / "env-001.json"
        entry = OutboxEntry.model_validate_json(entry_path.read_text())
        entry.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        entry_path.write_text(entry.model_dump_json(indent=2))

        result = outbox.retry_all()
        assert result["delivered"] == 1
        assert outbox.pending_count == 0

    def test_retry_exhausted_moves_to_dead(self, outbox: PersistentOutbox) -> None:
        """Messages past max retries go to dead letter."""
        outbox.enqueue("env-dead", "bob", _make_envelope_json("env-dead"))

        entry_path = outbox._pending / "env-dead.json"
        entry = OutboxEntry.model_validate_json(entry_path.read_text())
        entry.attempt_count = 3
        entry.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        entry_path.write_text(entry.model_dump_json(indent=2))

        result = outbox.retry_all()
        assert result["dead_lettered"] == 1
        assert outbox.pending_count == 0
        assert outbox.dead_count == 1


class TestListAndPurge:
    """Tests for listing and purging queues."""

    def test_list_pending(self, outbox: PersistentOutbox) -> None:
        """list_pending returns all queued entries."""
        for i in range(3):
            outbox.enqueue(f"env-{i}", "bob", _make_envelope_json(f"env-{i}"))
        entries = outbox.list_pending()
        assert len(entries) == 3

    def test_list_dead_empty(self, outbox: PersistentOutbox) -> None:
        """Empty dead letter returns empty list."""
        assert outbox.list_dead() == []

    def test_purge_pending(self, outbox: PersistentOutbox) -> None:
        """purge_pending removes all queued messages."""
        for i in range(4):
            outbox.enqueue(f"env-{i}", "bob", _make_envelope_json(f"env-{i}"))
        purged = outbox.purge_pending()
        assert purged == 4
        assert outbox.pending_count == 0

    def test_purge_dead(self, outbox: PersistentOutbox, tmp_path: Path) -> None:
        """purge_dead removes all dead-lettered messages."""
        dead_dir = tmp_path / "outbox" / "dead"
        (dead_dir / "env-dead.json").write_text(
            OutboxEntry(
                envelope_id="env-dead", recipient="bob",
                envelope_json="{}", attempt_count=10,
            ).model_dump_json()
        )
        purged = outbox.purge_dead()
        assert purged == 1
        assert outbox.dead_count == 0


class TestRequeueDead:
    """Tests for moving dead-lettered messages back to pending."""

    def test_requeue_all(self, outbox: PersistentOutbox) -> None:
        """requeue_dead moves all dead-lettered messages to pending."""
        dead_dir = outbox._dead
        for i in range(2):
            entry = OutboxEntry(
                envelope_id=f"env-{i}", recipient="bob",
                envelope_json=_make_envelope_json(f"env-{i}"),
                attempt_count=10,
            )
            (dead_dir / f"env-{i}.json").write_text(entry.model_dump_json())

        requeued = outbox.requeue_dead()
        assert requeued == 2
        assert outbox.dead_count == 0
        assert outbox.pending_count == 2

        pending = outbox.list_pending()
        assert all(e.attempt_count == 0 for e in pending)

    def test_requeue_specific(self, outbox: PersistentOutbox) -> None:
        """requeue_dead with ID only requeues that message."""
        for i in range(3):
            entry = OutboxEntry(
                envelope_id=f"env-{i}", recipient="bob",
                envelope_json="{}",
            )
            (outbox._dead / f"env-{i}.json").write_text(entry.model_dump_json())

        requeued = outbox.requeue_dead(envelope_id="env-1")
        assert requeued == 1
        assert outbox.dead_count == 2
        assert outbox.pending_count == 1


class TestBackoff:
    """Tests for exponential backoff computation."""

    def test_backoff_increases(self, outbox: PersistentOutbox) -> None:
        """Each attempt increases the backoff delay."""
        t1 = outbox._compute_next_retry(1)
        t2 = outbox._compute_next_retry(2)
        t3 = outbox._compute_next_retry(3)

        assert t2 > t1
        assert t3 > t2

    def test_backoff_caps_at_one_hour(self, outbox: PersistentOutbox) -> None:
        """Backoff doesn't exceed 1 hour regardless of attempt count."""
        t = outbox._compute_next_retry(100)
        now = datetime.now(timezone.utc)
        assert (t - now).total_seconds() <= 3601


class TestOutboxEntry:
    """Tests for the OutboxEntry model."""

    def test_defaults(self) -> None:
        """Entry has sensible defaults."""
        entry = OutboxEntry(
            envelope_id="test", recipient="bob", envelope_json="{}",
        )
        assert entry.attempt_count == 0
        assert entry.max_retries == 10
        assert entry.last_error == ""

    def test_serialization_roundtrip(self) -> None:
        """Entry survives JSON roundtrip."""
        entry = OutboxEntry(
            envelope_id="test", recipient="bob",
            envelope_json=_make_envelope_json(), attempt_count=3,
            last_error="timeout",
        )
        data = entry.model_dump_json()
        loaded = OutboxEntry.model_validate_json(data)
        assert loaded.envelope_id == "test"
        assert loaded.attempt_count == 3
