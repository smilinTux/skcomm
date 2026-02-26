"""Tests for SKComm REST API."""

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from skcomm.api import app, get_skcomm
from skcomm.models import (
    MessageEnvelope,
    MessageMetadata,
    MessagePayload,
    MessageType,
    RoutingConfig,
    RoutingMode,
    Urgency,
)
from skcomm.transport import DeliveryReport, SendResult


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def mock_skcomm():
    """Mock SKComm instance for testing."""
    with patch("skcomm.api.get_skcomm") as mock:
        mock_instance = Mock()
        mock_instance.identity = "test-agent"
        mock_instance.router.transports = []
        mock_instance.status.return_value = {
            "version": "1.0.0",
            "identity": {"name": "test-agent", "fingerprint": None},
            "default_mode": "failover",
            "transports": {},
            "transport_count": 0,
            "encrypt": True,
            "sign": True,
            "crypto": {
                "available": False,
                "encrypt_enabled": True,
                "sign_enabled": True,
                "fingerprint": None,
                "known_peers": [],
            },
        }
        mock.return_value = mock_instance
        yield mock_instance


class TestRootEndpoint:
    """Tests for the root endpoint."""

    def test_root_returns_service_info(self, client):
        """Expected: root endpoint returns service information."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "SKComm API"
        assert data["version"] == "0.1.0"
        assert data["status"] == "running"


class TestStatusEndpoint:
    """Tests for the /api/v1/status endpoint."""

    def test_status_returns_skcomm_status(self, client, mock_skcomm):
        """Expected: status endpoint returns SKComm configuration."""
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        data = response.json()
        assert "identity" in data
        assert "transports" in data
        assert data["identity"]["name"] == "test-agent"
        assert data["encrypt"] is True
        assert data["sign"] is True
        mock_skcomm.status.assert_called_once()


class TestSendEndpoint:
    """Tests for the /api/v1/send endpoint."""

    def test_send_message_success(self, client, mock_skcomm):
        """Expected: successful message send returns delivery report."""
        mock_report = DeliveryReport(
            envelope_id="test-envelope-123",
            delivered=True,
            attempts=[
                SendResult(
                    success=True,
                    transport_name="syncthing",
                    envelope_id="test-envelope-123",
                    latency_ms=10.5,
                )
            ],
        )
        mock_skcomm.send.return_value = mock_report

        response = client.post(
            "/api/v1/send",
            json={
                "recipient": "test-recipient",
                "message": "Hello from test",
                "message_type": "text",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["delivered"] is True
        assert data["envelope_id"] == "test-envelope-123"
        assert data["transport_used"] == "syncthing"
        assert len(data["attempts"]) == 1
        assert data["attempts"][0]["transport"] == "syncthing"
        assert data["attempts"][0]["success"] is True

    def test_send_message_failure(self, client, mock_skcomm):
        """Expected: failed message send returns error details."""
        mock_report = DeliveryReport(
            envelope_id="test-envelope-456",
            delivered=False,
            attempts=[
                SendResult(
                    success=False,
                    transport_name="syncthing",
                    envelope_id="test-envelope-456",
                    latency_ms=5.0,
                    error="Connection failed",
                )
            ],
        )
        mock_skcomm.send.return_value = mock_report

        response = client.post(
            "/api/v1/send",
            json={
                "recipient": "test-recipient",
                "message": "Hello from test",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["delivered"] is False
        assert data["envelope_id"] == "test-envelope-456"
        assert data["transport_used"] is None
        assert data["attempts"][0]["success"] is False
        assert data["attempts"][0]["error"] == "Connection failed"

    def test_send_message_with_optional_params(self, client, mock_skcomm):
        """Expected: send accepts optional routing and metadata params."""
        mock_report = DeliveryReport(
            envelope_id="test-envelope-789",
            delivered=True,
            attempts=[],
        )
        mock_skcomm.send.return_value = mock_report

        response = client.post(
            "/api/v1/send",
            json={
                "recipient": "test-recipient",
                "message": "Test message",
                "message_type": "command",
                "mode": "broadcast",
                "thread_id": "thread-123",
                "in_reply_to": "envelope-456",
                "urgency": "high",
            },
        )

        assert response.status_code == 200
        mock_skcomm.send.assert_called_once()
        call_kwargs = mock_skcomm.send.call_args.kwargs
        assert call_kwargs["recipient"] == "test-recipient"
        assert call_kwargs["message"] == "Test message"
        assert call_kwargs["message_type"] == MessageType.COMMAND
        assert call_kwargs["mode"] == RoutingMode.BROADCAST
        assert call_kwargs["thread_id"] == "thread-123"
        assert call_kwargs["in_reply_to"] == "envelope-456"
        assert call_kwargs["urgency"] == Urgency.HIGH


class TestInboxEndpoint:
    """Tests for the /api/v1/inbox endpoint."""

    def test_inbox_returns_empty_list(self, client, mock_skcomm):
        """Expected: inbox endpoint returns empty list when no messages."""
        mock_skcomm.receive.return_value = []

        response = client.get("/api/v1/inbox")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_inbox_returns_messages(self, client, mock_skcomm):
        """Expected: inbox endpoint returns received messages."""
        test_envelope = MessageEnvelope(
            sender="test-sender",
            recipient="test-agent",
            payload=MessagePayload(
                content="Test message content",
                content_type=MessageType.TEXT,
            ),
            routing=RoutingConfig(),
            metadata=MessageMetadata(urgency=Urgency.NORMAL),
        )
        mock_skcomm.receive.return_value = [test_envelope]

        response = client.get("/api/v1/inbox")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        msg = data[0]
        assert msg["sender"] == "test-sender"
        assert msg["recipient"] == "test-agent"
        assert msg["content"] == "Test message content"
        assert msg["content_type"] == "text"
        assert msg["urgency"] == "normal"
        assert msg["is_ack"] is False


class TestConversationsEndpoint:
    """Tests for the /api/v1/conversations endpoint."""

    def test_conversations_placeholder(self, client):
        """Expected: conversations endpoint returns empty list (placeholder)."""
        response = client.get("/api/v1/conversations")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0


class TestAgentsEndpoint:
    """Tests for the /api/v1/agents endpoint."""

    def test_agents_returns_known_peers(self, client, mock_skcomm):
        """Expected: agents endpoint returns known peers from keystore."""
        mock_skcomm.status.return_value = {
            "crypto": {
                "known_peers": ["agent-1", "agent-2"],
            }
        }

        response = client.get("/api/v1/agents")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "agent-1"
        assert data[1]["name"] == "agent-2"

    def test_agents_returns_empty_when_no_crypto(self, client, mock_skcomm):
        """Expected: agents endpoint returns empty list when crypto disabled."""
        mock_skcomm.status.return_value = {"crypto": {}}

        response = client.get("/api/v1/agents")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0


class TestPeersEndpoint:
    """Tests for the /api/v1/peers endpoint."""

    def test_get_peers_returns_empty_list(self, client, tmp_path, monkeypatch):
        """Expected: GET /peers returns empty list when no peers stored."""
        from skcomm import discovery as disc_mod

        monkeypatch.setattr(disc_mod, "SKCOMM_HOME", str(tmp_path))
        with monkeypatch.context() as m:
            from skcomm.discovery import PeerStore as PS

            m.setattr(PS, "__init__", lambda self, peers_dir=None: (
                setattr(self, "_dir", tmp_path / "peers") or
                (tmp_path / "peers").mkdir(parents=True, exist_ok=True)
            ))

            response = client.get("/api/v1/peers")
            assert response.status_code == 200
            assert isinstance(response.json(), list)

    def test_add_peer_success(self, client, tmp_path, monkeypatch):
        """Expected: POST /peers saves a peer and returns it."""
        from skcomm.discovery import PeerStore

        peers_dir = tmp_path / "peers"
        peers_dir.mkdir()

        original_init = PeerStore.__init__

        def patched_init(self, peers_dir_arg=None):
            original_init(self, peers_dir=peers_dir)

        monkeypatch.setattr(PeerStore, "__init__", patched_init)

        response = client.post(
            "/api/v1/peers",
            json={
                "name": "lumina",
                "address": "/home/user/.skcapstone/comms",
                "transport": "syncthing",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "lumina"
        assert data["discovered_via"] == "manual"
        assert len(data["transports"]) == 1
        assert data["transports"][0]["transport"] == "syncthing"

    def test_add_peer_with_fingerprint(self, client, tmp_path, monkeypatch):
        """Expected: POST /peers with fingerprint saves it."""
        from skcomm.discovery import PeerStore

        peers_dir = tmp_path / "peers"
        peers_dir.mkdir()
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir_arg=None):
            original_init(self, peers_dir=peers_dir)

        monkeypatch.setattr(PeerStore, "__init__", patched_init)

        response = client.post(
            "/api/v1/peers",
            json={
                "name": "opus",
                "address": "/shared/inbox",
                "transport": "file",
                "fingerprint": "DEADBEEF1234",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["fingerprint"] == "DEADBEEF1234"

    def test_remove_peer_not_found(self, client, tmp_path, monkeypatch):
        """Edge case: DELETE /peers/<name> returns 404 when peer does not exist."""
        from skcomm.discovery import PeerStore

        peers_dir = tmp_path / "peers"
        peers_dir.mkdir()
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir_arg=None):
            original_init(self, peers_dir=peers_dir)

        monkeypatch.setattr(PeerStore, "__init__", patched_init)

        response = client.delete("/api/v1/peers/nonexistent")
        assert response.status_code == 404

    def test_get_peers_returns_saved_peers(self, client, tmp_path, monkeypatch):
        """Expected: GET /peers returns peers saved via POST."""
        from skcomm.discovery import PeerStore

        peers_dir = tmp_path / "peers"
        peers_dir.mkdir()
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir_arg=None):
            original_init(self, peers_dir=peers_dir)

        monkeypatch.setattr(PeerStore, "__init__", patched_init)

        # Add a peer first
        client.post("/api/v1/peers", json={"name": "hal", "address": "/comms", "transport": "syncthing"})

        response = client.get("/api/v1/peers")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "hal"


class TestPresenceEndpoint:
    """Tests for the /api/v1/presence endpoint."""

    def test_presence_update(self, client, mock_skcomm):
        """Expected: presence update returns confirmation."""
        response = client.post(
            "/api/v1/presence",
            json={
                "status": "online",
                "message": "Working on tests",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "online"
        assert data["message"] == "Working on tests"
        assert data["identity"] == "test-agent"
        assert "updated_at" in data

    def test_presence_update_without_message(self, client, mock_skcomm):
        """Expected: presence update works without optional message."""
        response = client.post(
            "/api/v1/presence",
            json={"status": "away"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "away"
        assert data["message"] is None
