"""Tests for the Syncthing transport."""

import json

import pytest

from skcomm.models import MessageEnvelope, MessagePayload
from skcomm.transport import TransportStatus
from skcomm.transports.syncthing import (
    ENVELOPE_SUFFIX,
    SyncthingTransport,
    create_transport,
)


@pytest.fixture
def comms_root(tmp_path):
    """Temporary comms directory for testing."""
    return tmp_path / "comms"


@pytest.fixture
def transport(comms_root):
    """A SyncthingTransport pointed at a temp directory."""
    return SyncthingTransport(comms_root=comms_root, priority=1, archive=True)


@pytest.fixture
def envelope():
    """A sample envelope for testing."""
    return MessageEnvelope(
        sender="opus",
        recipient="lumina",
        payload=MessagePayload(content="Hello via Syncthing"),
    )


class TestSyncthingTransportInit:
    """Tests for transport initialization."""

    def test_default_init(self):
        """Expected: defaults to ~/.skcapstone/comms/."""
        t = SyncthingTransport()
        assert "skcapstone/comms" in str(t._root)
        assert t.name == "syncthing"
        assert t.priority == 1

    def test_custom_root(self, comms_root):
        """Expected: custom comms_root is respected."""
        t = SyncthingTransport(comms_root=comms_root, priority=5)
        assert t._root == comms_root
        assert t.priority == 5

    def test_configure(self, comms_root):
        """Expected: configure() updates root paths."""
        t = SyncthingTransport()
        t.configure({"comms_root": str(comms_root), "archive": False})
        assert t._root == comms_root
        assert t._archive is False


class TestSyncthingTransportAvailability:
    """Tests for is_available() and health_check()."""

    def test_is_available(self, transport):
        """Expected: available after dirs are created."""
        assert transport.is_available() is True

    def test_is_available_creates_dirs(self, transport, comms_root):
        """Expected: is_available creates directory structure."""
        transport.is_available()
        assert (comms_root / "outbox").exists()
        assert (comms_root / "inbox").exists()
        assert (comms_root / "archive").exists()

    def test_health_check_available(self, transport):
        """Expected: healthy transport reports available."""
        health = transport.health_check()
        assert health.status == TransportStatus.AVAILABLE
        assert health.latency_ms is not None
        assert health.error is None
        assert "comms_root" in health.details

    def test_health_check_reports_peers(self, transport, comms_root):
        """Expected: health check shows inbox/outbox peer dirs."""
        (comms_root / "outbox" / "lumina").mkdir(parents=True)
        (comms_root / "inbox" / "opus").mkdir(parents=True)

        health = transport.health_check()
        assert "lumina" in health.details["outbox_peers"]
        assert "opus" in health.details["inbox_peers"]


class TestSyncthingTransportSend:
    """Tests for sending envelopes."""

    def test_send_creates_file(self, transport, comms_root, envelope):
        """Expected: envelope written as JSON file in outbox/recipient/."""
        result = transport.send(envelope.to_bytes(), "lumina")
        assert result.success is True
        assert result.transport_name == "syncthing"

        outbox = comms_root / "outbox" / "lumina"
        files = list(outbox.glob(f"*{ENVELOPE_SUFFIX}"))
        assert len(files) == 1

        data = json.loads(files[0].read_bytes())
        assert data["sender"] == "opus"
        assert data["payload"]["content"] == "Hello via Syncthing"

    def test_send_filename_includes_envelope_id(self, transport, comms_root, envelope):
        """Expected: filename is {envelope_id}.skc.json."""
        transport.send(envelope.to_bytes(), "lumina")
        outbox = comms_root / "outbox" / "lumina"
        files = list(outbox.glob(f"*{ENVELOPE_SUFFIX}"))
        assert envelope.envelope_id in files[0].name

    def test_send_creates_peer_directory(self, transport, comms_root):
        """Expected: peer directory auto-created on first send."""
        data = MessageEnvelope(
            sender="opus",
            recipient="new_peer",
            payload=MessagePayload(content="first contact"),
        ).to_bytes()

        transport.send(data, "new_peer")
        assert (comms_root / "outbox" / "new_peer").is_dir()

    def test_send_multiple_to_same_peer(self, transport, comms_root):
        """Expected: multiple envelopes coexist in same peer dir."""
        for i in range(3):
            env = MessageEnvelope(
                sender="opus",
                recipient="lumina",
                payload=MessagePayload(content=f"msg-{i}"),
            )
            transport.send(env.to_bytes(), "lumina")

        files = list((comms_root / "outbox" / "lumina").glob(f"*{ENVELOPE_SUFFIX}"))
        assert len(files) == 3

    def test_send_atomic_write(self, transport, comms_root, envelope):
        """Expected: no .tmp files left after send."""
        transport.send(envelope.to_bytes(), "lumina")
        outbox = comms_root / "outbox" / "lumina"
        tmp_files = list(outbox.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_send_returns_latency(self, transport, envelope):
        """Expected: send result includes timing data."""
        result = transport.send(envelope.to_bytes(), "lumina")
        assert result.latency_ms >= 0


class TestSyncthingTransportReceive:
    """Tests for receiving envelopes."""

    def test_receive_picks_up_files(self, transport, comms_root):
        """Expected: envelopes in inbox are received."""
        inbox = comms_root / "inbox" / "opus"
        inbox.mkdir(parents=True)

        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="incoming"),
        )
        (inbox / f"{env.envelope_id}{ENVELOPE_SUFFIX}").write_bytes(env.to_bytes())

        received = transport.receive()
        assert len(received) == 1

        parsed = MessageEnvelope.from_bytes(received[0])
        assert parsed.payload.content == "incoming"

    def test_receive_multiple_peers(self, transport, comms_root):
        """Expected: envelopes from multiple peers are all received."""
        for peer in ["opus", "jarvis"]:
            inbox = comms_root / "inbox" / peer
            inbox.mkdir(parents=True)
            env = MessageEnvelope(
                sender=peer,
                recipient="lumina",
                payload=MessagePayload(content=f"from {peer}"),
            )
            (inbox / f"{env.envelope_id}{ENVELOPE_SUFFIX}").write_bytes(env.to_bytes())

        received = transport.receive()
        assert len(received) == 2

    def test_receive_archives_processed(self, transport, comms_root):
        """Expected: processed files moved to archive/."""
        inbox = comms_root / "inbox" / "opus"
        inbox.mkdir(parents=True)
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="archive me"),
        )
        (inbox / f"{env.envelope_id}{ENVELOPE_SUFFIX}").write_bytes(env.to_bytes())

        transport.receive()

        inbox_files = list(inbox.glob(f"*{ENVELOPE_SUFFIX}"))
        archive_files = list((comms_root / "archive").glob(f"*{ENVELOPE_SUFFIX}"))
        assert len(inbox_files) == 0
        assert len(archive_files) == 1

    def test_receive_deletes_without_archive(self, comms_root):
        """Expected: without archive mode, files are deleted."""
        t = SyncthingTransport(comms_root=comms_root, archive=False)
        inbox = comms_root / "inbox" / "opus"
        inbox.mkdir(parents=True)

        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="delete me"),
        )
        (inbox / f"{env.envelope_id}{ENVELOPE_SUFFIX}").write_bytes(env.to_bytes())

        t.receive()

        inbox_files = list(inbox.glob(f"*{ENVELOPE_SUFFIX}"))
        assert len(inbox_files) == 0
        assert not (comms_root / "archive").exists()

    def test_receive_skips_hidden_files(self, transport, comms_root):
        """Edge case: files starting with . are skipped."""
        inbox = comms_root / "inbox" / "opus"
        inbox.mkdir(parents=True)
        (inbox / f".temp{ENVELOPE_SUFFIX}").write_bytes(b'{"partial": true}')

        received = transport.receive()
        assert len(received) == 0

    def test_receive_empty_inbox(self, transport):
        """Expected: empty inbox returns empty list."""
        received = transport.receive()
        assert received == []


class TestSyncthingTransportPending:
    """Tests for pending file listing."""

    def test_pending_outbox(self, transport, comms_root, envelope):
        """Expected: pending_outbox lists unsent files."""
        transport.send(envelope.to_bytes(), "lumina")
        pending = transport.pending_outbox()
        assert len(pending) == 1

    def test_pending_outbox_by_peer(self, transport, comms_root):
        """Expected: pending_outbox filtered by peer."""
        for peer in ["lumina", "jarvis"]:
            env = MessageEnvelope(
                sender="opus",
                recipient=peer,
                payload=MessagePayload(content=f"to {peer}"),
            )
            transport.send(env.to_bytes(), peer)

        assert len(transport.pending_outbox("lumina")) == 1
        assert len(transport.pending_outbox("jarvis")) == 1
        assert len(transport.pending_outbox("nobody")) == 0

    def test_pending_inbox(self, transport, comms_root):
        """Expected: pending_inbox lists unreceived files."""
        inbox = comms_root / "inbox" / "opus"
        inbox.mkdir(parents=True)
        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="pending"),
        )
        (inbox / f"{env.envelope_id}{ENVELOPE_SUFFIX}").write_bytes(env.to_bytes())

        assert len(transport.pending_inbox()) == 1
        assert len(transport.pending_inbox("opus")) == 1


class TestSyncthingRoundTrip:
    """End-to-end tests simulating agent-to-agent messaging."""

    def test_send_then_receive(self, comms_root):
        """Expected: message sent by agent A is received by agent B."""
        agent_a = SyncthingTransport(comms_root=comms_root, archive=True)
        agent_b = SyncthingTransport(comms_root=comms_root, archive=True)

        env = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="Cross-agent message"),
        )

        agent_a.send(env.to_bytes(), "lumina")

        # Simulate Syncthing sync: move file from outbox to inbox
        outbox_file = list(
            (comms_root / "outbox" / "lumina").glob(f"*{ENVELOPE_SUFFIX}")
        )[0]
        inbox_dir = comms_root / "inbox" / "opus"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        import shutil
        shutil.copy(str(outbox_file), str(inbox_dir / outbox_file.name))

        received = agent_b.receive()
        assert len(received) == 1
        parsed = MessageEnvelope.from_bytes(received[0])
        assert parsed.sender == "opus"
        assert parsed.payload.content == "Cross-agent message"

    def test_bidirectional_conversation(self, comms_root):
        """Expected: two agents can exchange messages back and forth."""
        import shutil

        opus = SyncthingTransport(comms_root=comms_root, archive=True)
        lumina = SyncthingTransport(comms_root=comms_root, archive=True)

        msg1 = MessageEnvelope(
            sender="opus",
            recipient="lumina",
            payload=MessagePayload(content="Hey Lumina"),
        )
        opus.send(msg1.to_bytes(), "lumina")

        outfile = list((comms_root / "outbox" / "lumina").glob(f"*{ENVELOPE_SUFFIX}"))[0]
        inbox_dir = comms_root / "inbox" / "opus"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(outfile), str(inbox_dir / outfile.name))

        received = lumina.receive()
        assert len(received) == 1
        assert MessageEnvelope.from_bytes(received[0]).payload.content == "Hey Lumina"

        msg2 = MessageEnvelope(
            sender="lumina",
            recipient="opus",
            payload=MessagePayload(content="Hey Opus!"),
        )
        lumina.send(msg2.to_bytes(), "opus")

        outfile2 = list((comms_root / "outbox" / "opus").glob(f"*{ENVELOPE_SUFFIX}"))[0]
        inbox_dir2 = comms_root / "inbox" / "lumina"
        inbox_dir2.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(outfile2), str(inbox_dir2 / outfile2.name))

        received2 = opus.receive()
        assert len(received2) == 1
        assert MessageEnvelope.from_bytes(received2[0]).payload.content == "Hey Opus!"


class TestCreateTransportFactory:
    """Tests for the create_transport factory function."""

    def test_factory_default(self):
        """Expected: factory creates transport with defaults."""
        t = create_transport()
        assert isinstance(t, SyncthingTransport)
        assert t.priority == 1

    def test_factory_custom(self, tmp_path):
        """Expected: factory accepts custom parameters."""
        t = create_transport(priority=3, comms_root=str(tmp_path), archive=False)
        assert t.priority == 3
        assert t._root == tmp_path
        assert t._archive is False
