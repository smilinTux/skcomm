"""Tests for message priority queue in SKComm.

Verifies that:
- MessageEnvelope.priority maps urgency to the correct integer.
- MessagePriorityQueue drains in CRITICAL→HIGH→NORMAL→LOW order.
- FIFO ordering is preserved within the same priority tier.
- SKComm.receive() returns messages ordered by urgency.
"""

import pytest

from skcomm.core import MessagePriorityQueue, SKComm
from skcomm.models import MessageEnvelope, MessageMetadata, MessagePayload, Urgency
from skcomm.router import Router

from .conftest import MockTransport


def _envelope(urgency: Urgency, content: str = "test") -> MessageEnvelope:
    return MessageEnvelope(
        sender="opus",
        recipient="lumina",
        payload=MessagePayload(content=content),
        metadata=MessageMetadata(urgency=urgency),
    )


class TestEnvelopePriority:
    """MessageEnvelope.priority maps urgency to the expected integer."""

    def test_critical_is_zero(self):
        assert _envelope(Urgency.CRITICAL).priority == 0

    def test_high_is_one(self):
        assert _envelope(Urgency.HIGH).priority == 1

    def test_normal_is_two(self):
        assert _envelope(Urgency.NORMAL).priority == 2

    def test_low_is_three(self):
        assert _envelope(Urgency.LOW).priority == 3

    def test_lower_number_means_higher_urgency(self):
        """CRITICAL < HIGH < NORMAL < LOW as integers."""
        priorities = [
            _envelope(Urgency.CRITICAL).priority,
            _envelope(Urgency.HIGH).priority,
            _envelope(Urgency.NORMAL).priority,
            _envelope(Urgency.LOW).priority,
        ]
        assert priorities == sorted(priorities)


class TestMessagePriorityQueue:
    """MessagePriorityQueue dequeues in urgency order."""

    def test_critical_dequeued_before_normal(self):
        q = MessagePriorityQueue()
        q.push(_envelope(Urgency.NORMAL, "normal"))
        q.push(_envelope(Urgency.CRITICAL, "critical"))
        assert q.pop().metadata.urgency == Urgency.CRITICAL

    def test_drain_full_order(self):
        q = MessagePriorityQueue()
        q.push(_envelope(Urgency.LOW, "low"))
        q.push(_envelope(Urgency.CRITICAL, "critical"))
        q.push(_envelope(Urgency.NORMAL, "normal"))
        q.push(_envelope(Urgency.HIGH, "high"))

        result = q.drain()
        urgencies = [e.metadata.urgency for e in result]
        assert urgencies == [Urgency.CRITICAL, Urgency.HIGH, Urgency.NORMAL, Urgency.LOW]

    def test_fifo_within_same_priority(self):
        q = MessagePriorityQueue()
        q.push(_envelope(Urgency.HIGH, "first"))
        q.push(_envelope(Urgency.HIGH, "second"))
        q.push(_envelope(Urgency.HIGH, "third"))

        contents = [e.payload.content for e in q.drain()]
        assert contents == ["first", "second", "third"]

    def test_mixed_fifo_and_priority(self):
        """Interleaved pushes: priority wins, FIFO breaks ties."""
        q = MessagePriorityQueue()
        q.push(_envelope(Urgency.LOW, "a"))
        q.push(_envelope(Urgency.CRITICAL, "b"))
        q.push(_envelope(Urgency.LOW, "c"))
        q.push(_envelope(Urgency.CRITICAL, "d"))

        contents = [e.payload.content for e in q.drain()]
        # Both CRITICALs come first in insertion order, then LOWs in order
        assert contents == ["b", "d", "a", "c"]

    def test_len_updates(self):
        q = MessagePriorityQueue()
        assert len(q) == 0
        q.push(_envelope(Urgency.NORMAL))
        assert len(q) == 1
        q.push(_envelope(Urgency.CRITICAL))
        assert len(q) == 2
        q.pop()
        assert len(q) == 1

    def test_drain_empties_queue(self):
        q = MessagePriorityQueue()
        q.push(_envelope(Urgency.NORMAL))
        q.push(_envelope(Urgency.HIGH))
        q.drain()
        assert len(q) == 0

    def test_drain_empty_returns_empty_list(self):
        assert MessagePriorityQueue().drain() == []

    def test_pop_empty_raises(self):
        with pytest.raises(IndexError):
            MessagePriorityQueue().pop()


class TestSKCommReceivePriority:
    """SKComm.receive() returns envelopes sorted by urgency."""

    def _comm_with_messages(self, urgencies: list[Urgency]) -> SKComm:
        mock = MockTransport(name="mock", priority=1)
        for u in urgencies:
            mock.queue_message(_envelope(u, u.value).to_bytes())
        router = Router(transports=[mock])
        return SKComm(router=router)

    def test_receive_critical_first(self):
        comm = self._comm_with_messages(
            [Urgency.LOW, Urgency.CRITICAL, Urgency.NORMAL, Urgency.HIGH]
        )
        messages = comm.receive()
        urgencies = [m.metadata.urgency for m in messages]
        assert urgencies == [Urgency.CRITICAL, Urgency.HIGH, Urgency.NORMAL, Urgency.LOW]

    def test_receive_already_sorted_unchanged(self):
        comm = self._comm_with_messages(
            [Urgency.CRITICAL, Urgency.HIGH, Urgency.NORMAL, Urgency.LOW]
        )
        messages = comm.receive()
        urgencies = [m.metadata.urgency for m in messages]
        assert urgencies == [Urgency.CRITICAL, Urgency.HIGH, Urgency.NORMAL, Urgency.LOW]

    def test_receive_all_critical(self):
        comm = self._comm_with_messages(
            [Urgency.CRITICAL, Urgency.CRITICAL, Urgency.CRITICAL]
        )
        messages = comm.receive()
        assert all(m.metadata.urgency == Urgency.CRITICAL for m in messages)
        assert len(messages) == 3

    def test_receive_single_message(self):
        comm = self._comm_with_messages([Urgency.HIGH])
        messages = comm.receive()
        assert len(messages) == 1
        assert messages[0].metadata.urgency == Urgency.HIGH

    def test_receive_empty(self):
        comm = self._comm_with_messages([])
        assert comm.receive() == []
