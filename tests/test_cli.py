"""Tests for the SKComm CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skcomm.cli import main


@pytest.fixture
def runner():
    """Provide a Click CliRunner for invoking commands."""
    return CliRunner()


class TestCLIBasics:
    """Test basic CLI structure and help."""

    def test_version(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "skcomm" in result.output.lower()

    def test_help(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "send" in result.output
        assert "receive" in result.output
        assert "status" in result.output

    def test_send_help(self, runner):
        result = runner.invoke(main, ["send", "--help"])
        assert result.exit_code == 0
        assert "RECIPIENT" in result.output
        assert "MESSAGE" in result.output

    def test_receive_help(self, runner):
        result = runner.invoke(main, ["receive", "--help"])
        assert result.exit_code == 0

    def test_status_help(self, runner):
        result = runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_peers_help(self, runner):
        result = runner.invoke(main, ["peers", "--help"])
        assert result.exit_code == 0


class TestInitCommand:
    """Test the init command."""

    def test_init_creates_config(self, runner, tmp_path):
        with patch("skcomm.cli._HOME", str(tmp_path)):
            result = runner.invoke(main, ["init", "--name", "testbot"])
            assert result.exit_code == 0
            assert "ready" in result.output.lower() or "initialized" in result.output.lower()

            config_path = tmp_path / "config.yml"
            assert config_path.exists()

            import yaml

            config = yaml.safe_load(config_path.read_text())
            assert config["skcomm"]["identity"]["name"] == "testbot"
            assert "syncthing" in config["skcomm"]["transports"]
            assert "file" in config["skcomm"]["transports"]

    def test_init_creates_directories(self, runner, tmp_path):
        with patch("skcomm.cli._HOME", str(tmp_path)):
            runner.invoke(main, ["init", "--name", "testbot"])
            assert (tmp_path / "logs").is_dir()
            assert (tmp_path / "filedrop" / "inbox").is_dir()
            assert (tmp_path / "filedrop" / "outbox").is_dir()


class TestSendCommand:
    """Test the send command with mocked SKComm."""

    def test_send_success(self, runner):
        mock_report = MagicMock()
        mock_report.delivered = True
        mock_report.successful_transport = "file"
        mock_report.attempts = [
            MagicMock(success=True, transport_name="file", latency_ms=1.5)
        ]

        mock_comm = MagicMock()
        mock_comm.send.return_value = mock_report

        with patch("skcomm.core.SKComm.from_config", return_value=mock_comm):
            result = runner.invoke(main, ["send", "lumina", "Hello there"])
            assert result.exit_code == 0
            assert "lumina" in result.output

    def test_send_failure(self, runner):
        mock_report = MagicMock()
        mock_report.delivered = False
        mock_report.attempts = [
            MagicMock(
                success=False, transport_name="file", error="Connection refused"
            )
        ]

        mock_comm = MagicMock()
        mock_comm.send.return_value = mock_report

        with patch("skcomm.core.SKComm.from_config", return_value=mock_comm):
            result = runner.invoke(main, ["send", "lumina", "Hello there"])
            assert result.exit_code == 1

    def test_send_with_urgency(self, runner):
        mock_report = MagicMock()
        mock_report.delivered = True
        mock_report.successful_transport = "file"
        mock_report.attempts = [
            MagicMock(success=True, transport_name="file", latency_ms=2.0)
        ]

        mock_comm = MagicMock()
        mock_comm.send.return_value = mock_report

        with patch("skcomm.core.SKComm.from_config", return_value=mock_comm):
            result = runner.invoke(
                main, ["send", "lumina", "urgent msg", "-u", "critical"]
            )
            assert result.exit_code == 0


class TestReceiveCommand:
    """Test the receive command."""

    def test_receive_no_messages(self, runner):
        mock_comm = MagicMock()
        mock_comm.receive.return_value = []

        with patch("skcomm.core.SKComm.from_config", return_value=mock_comm):
            result = runner.invoke(main, ["receive"])
            assert result.exit_code == 0
            assert "no new" in result.output.lower() or "No new" in result.output

    def test_receive_json_output(self, runner):
        mock_env = MagicMock()
        mock_env.model_dump_json.return_value = '{"envelope_id": "test"}'

        mock_comm = MagicMock()
        mock_comm.receive.return_value = [mock_env]

        with patch("skcomm.core.SKComm.from_config", return_value=mock_comm):
            result = runner.invoke(main, ["receive", "--json-out"])
            assert result.exit_code == 0
            assert "envelope_id" in result.output


class TestStatusCommand:
    """Test the status command."""

    def test_status_json_output(self, runner):
        mock_comm = MagicMock()
        mock_comm.status.return_value = {
            "version": "1.0.0",
            "identity": {"name": "opus"},
            "default_mode": "failover",
            "encrypt": True,
            "sign": True,
            "transport_count": 2,
            "transports": {},
        }

        with patch("skcomm.core.SKComm.from_config", return_value=mock_comm):
            result = runner.invoke(main, ["status", "--json-out"])
            assert result.exit_code == 0
            parsed = json.loads(result.output)
            assert parsed["identity"]["name"] == "opus"


class TestPeersCommand:
    """Test the peers command."""

    def test_peers_no_peers(self, runner, tmp_path):
        mock_store = MagicMock()
        mock_store.return_value.list_all.return_value = []

        with patch("skcomm.discovery.PeerStore", mock_store):
            result = runner.invoke(main, ["peers"])
            assert result.exit_code == 0
            assert "no peers" in result.output.lower() or "No peers" in result.output


class TestPeerGroup:
    """Test the peer subcommand group (add, remove, list)."""

    def test_peer_add_syncthing(self, runner, tmp_path):
        """Expected: peer add stores a syncthing peer."""
        from skcomm.discovery import PeerStore
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir=None):
            original_init(self, peers_dir=tmp_path / "peers")

        with patch.object(PeerStore, "__init__", patched_init):
            result = runner.invoke(
                main,
                ["peer", "add", "lumina", "/home/user/.skcapstone/comms", "--transport", "syncthing"],
            )
            assert result.exit_code == 0
            assert "lumina" in result.output
            assert "syncthing" in result.output.lower()

    def test_peer_add_file_transport(self, runner, tmp_path):
        """Expected: peer add with file transport stores address."""
        from skcomm.discovery import PeerStore
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir=None):
            original_init(self, peers_dir=tmp_path / "peers")

        with patch.object(PeerStore, "__init__", patched_init):
            result = runner.invoke(
                main,
                ["peer", "add", "opus", "/mnt/shared/inbox", "--transport", "file"],
            )
            assert result.exit_code == 0
            assert "opus" in result.output

    def test_peer_list_empty(self, runner, tmp_path):
        """Expected: peer list shows empty message when no peers."""
        from skcomm.discovery import PeerStore
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir=None):
            original_init(self, peers_dir=tmp_path / "peers")

        with patch.object(PeerStore, "__init__", patched_init):
            result = runner.invoke(main, ["peer", "list"])
            assert result.exit_code == 0
            assert "no peers" in result.output.lower()

    def test_peer_list_shows_added_peer(self, runner, tmp_path):
        """Expected: peer list displays peers after adding them."""
        from skcomm.discovery import PeerStore
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir=None):
            original_init(self, peers_dir=tmp_path / "peers")

        with patch.object(PeerStore, "__init__", patched_init):
            runner.invoke(
                main,
                ["peer", "add", "hal9000", "/comms", "--transport", "syncthing"],
            )
            result = runner.invoke(main, ["peer", "list"])
            assert result.exit_code == 0
            assert "hal9000" in result.output

    def test_peer_remove_nonexistent(self, runner, tmp_path):
        """Edge case: peer remove of missing peer shows warning."""
        from skcomm.discovery import PeerStore
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir=None):
            original_init(self, peers_dir=tmp_path / "peers")

        with patch.object(PeerStore, "__init__", patched_init):
            result = runner.invoke(main, ["peer", "remove", "nobody"])
            assert result.exit_code == 0
            assert "not found" in result.output.lower()

    def test_peer_add_with_fingerprint(self, runner, tmp_path):
        """Expected: peer add stores optional fingerprint."""
        from skcomm.discovery import PeerStore
        original_init = PeerStore.__init__

        def patched_init(self, peers_dir=None):
            original_init(self, peers_dir=tmp_path / "peers")

        with patch.object(PeerStore, "__init__", patched_init):
            result = runner.invoke(
                main,
                ["peer", "add", "secure-peer", "/comms", "--fingerprint", "DEADBEEF1234"],
            )
            assert result.exit_code == 0
            assert "DEADBEEF1234" in result.output
