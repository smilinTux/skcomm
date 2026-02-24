"""Tests for the file transport — local filesystem message drops."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from skcomm.transports.file import FileTransport, create_transport


def _make_envelope_bytes(
    sender: str = "opus",
    recipient: str = "lumina",
    content: str = "test message",
    envelope_id: str | None = None,
) -> bytes:
    """Create minimal valid envelope bytes for testing."""
    return json.dumps(
        {
            "skcomm_version": "1.0.0",
            "envelope_id": envelope_id or str(uuid.uuid4()),
            "sender": sender,
            "recipient": recipient,
            "payload": {"content": content, "content_type": "text"},
            "routing": {"mode": "failover"},
            "metadata": {"urgency": "normal"},
        }
    ).encode()


class TestFileTransportInit:
    """Test FileTransport initialization and configuration."""

    def test_default_paths(self):
        t = FileTransport()
        assert t.name == "file"
        assert t.priority == 2
        assert "skcomm" in str(t._outbox)
        assert "skcomm" in str(t._inbox)

    def test_custom_paths(self, tmp_path):
        outbox = tmp_path / "out"
        inbox = tmp_path / "in"
        t = FileTransport(outbox_path=outbox, inbox_path=inbox)
        assert t._outbox == outbox
        assert t._inbox == inbox

    def test_custom_priority(self, tmp_path):
        t = FileTransport(outbox_path=tmp_path / "out", priority=5)
        assert t.priority == 5

    def test_configure_overrides(self, tmp_path):
        t = FileTransport(outbox_path=tmp_path / "out", inbox_path=tmp_path / "in")
        custom_inbox = tmp_path / "custom_in"
        t.configure({"inbox_path": str(custom_inbox)})
        assert t._inbox == custom_inbox

    def test_factory_function(self, tmp_path):
        t = create_transport(
            priority=3,
            outbox_path=str(tmp_path / "out"),
            inbox_path=str(tmp_path / "in"),
        )
        assert isinstance(t, FileTransport)
        assert t.priority == 3
        assert t._outbox == tmp_path / "out"


class TestFileTransportAvailability:
    """Test health and availability checks."""

    def test_is_available_creates_dirs(self, tmp_path):
        outbox = tmp_path / "newdrop" / "outbox"
        inbox = tmp_path / "newdrop" / "inbox"
        t = FileTransport(outbox_path=outbox, inbox_path=inbox)
        assert t.is_available()
        assert outbox.is_dir()
        assert inbox.is_dir()

    def test_health_check_available(self, tmp_path):
        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=tmp_path / "inbox",
        )
        health = t.health_check()
        assert health.status.value == "available"
        assert health.transport_name == "file"
        assert health.details["pending_outbox"] == 0

    def test_health_check_with_messages(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "test.skc.json").write_bytes(b'{"test": true}')

        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=inbox,
        )
        health = t.health_check()
        assert health.details["pending_inbox"] == 1


class TestFileTransportSendReceive:
    """Test the send/receive round-trip."""

    def test_send_creates_file(self, tmp_path):
        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=tmp_path / "inbox",
            archive=False,
        )
        data = _make_envelope_bytes(envelope_id="test-id-123")
        result = t.send(data, "lumina")

        assert result.success
        assert result.transport_name == "file"
        assert result.envelope_id == "test-id-123"

        files = list((tmp_path / "outbox").glob("*.skc.json"))
        assert len(files) == 1
        assert "test-id-123" in files[0].name

    def test_receive_empty_inbox(self, tmp_path):
        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=tmp_path / "inbox",
        )
        assert t.receive() == []

    def test_send_and_receive_roundtrip(self, tmp_path):
        """Simulate A sends, B receives by sharing the same directory."""
        outbox = tmp_path / "outbox"
        inbox = tmp_path / "inbox"
        sender = FileTransport(outbox_path=outbox, inbox_path=inbox, archive=False)
        data = _make_envelope_bytes(content="hello there")

        result = sender.send(data, "lumina")
        assert result.success

        outbox_files = list(outbox.glob("*.skc.json"))
        assert len(outbox_files) == 1

        # Reason: simulate delivery by moving outbox -> inbox
        inbox.mkdir(exist_ok=True)
        for f in outbox_files:
            f.rename(inbox / f.name)

        receiver = FileTransport(outbox_path=outbox, inbox_path=inbox, archive=False)
        received = receiver.receive()
        assert len(received) == 1

        parsed = json.loads(received[0])
        assert parsed["payload"]["content"] == "hello there"

    def test_receive_archives_by_default(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)
        archive = tmp_path / "archive"

        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=inbox,
            archive=True,
            archive_path=archive,
        )

        env_file = inbox / "msg.skc.json"
        env_file.write_bytes(_make_envelope_bytes())

        received = t.receive()
        assert len(received) == 1
        assert not env_file.exists()

        archive_files = list(archive.glob("*.skc.json"))
        assert len(archive_files) == 1

    def test_receive_deletes_without_archive(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)

        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=inbox,
            archive=False,
        )

        env_file = inbox / "msg.skc.json"
        env_file.write_bytes(_make_envelope_bytes())

        t.receive()
        assert not env_file.exists()

    def test_multiple_messages(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)

        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=inbox,
            archive=False,
        )

        for i in range(5):
            (inbox / f"msg-{i}.skc.json").write_bytes(
                _make_envelope_bytes(content=f"msg {i}")
            )

        received = t.receive()
        assert len(received) == 5


class TestFileTransportEdgeCases:
    """Test error handling and edge cases."""

    def test_send_invalid_json_uses_fallback_id(self, tmp_path):
        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=tmp_path / "inbox",
        )
        result = t.send(b"not json", "lumina")
        assert result.success
        assert "unknown" in result.envelope_id

    def test_skips_dotfiles(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)

        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=inbox,
        )

        (inbox / ".hidden.skc.json").write_bytes(b'{"hidden": true}')
        (inbox / "visible.skc.json").write_bytes(_make_envelope_bytes())

        received = t.receive()
        assert len(received) == 1

    def test_archive_collision_handling(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)
        archive = tmp_path / "archive"
        archive.mkdir(parents=True)

        t = FileTransport(
            outbox_path=tmp_path / "outbox",
            inbox_path=inbox,
            archive=True,
            archive_path=archive,
        )

        (inbox / "dup.skc.json").write_bytes(_make_envelope_bytes())
        (archive / "dup.skc.json").write_bytes(b"old")

        t.receive()
        archive_files = list(archive.glob("*.skc.json"))
        assert len(archive_files) == 2
