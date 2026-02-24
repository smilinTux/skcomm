"""Tests for SKComm configuration loading."""

import pytest

from skcomm.config import SKCommConfig, load_config
from skcomm.models import RoutingMode


class TestSKCommConfig:
    """Tests for the SKCommConfig model."""

    def test_default_config(self):
        """Expected: sensible defaults when no config file exists."""
        config = SKCommConfig()
        assert config.version == "1.0.0"
        assert config.identity.name == "unknown"
        assert config.default_mode == RoutingMode.FAILOVER
        assert config.encrypt is True
        assert config.sign is True
        assert config.ack is True
        assert config.retry_max == 5
        assert config.ttl == 86400
        assert config.transports == {}

    def test_config_from_yaml(self, tmp_path):
        """Expected: config loaded from YAML file."""
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            """
skcomm:
  version: "1.0.0"
  identity:
    name: "opus"
    fingerprint: "ABCD1234"
  defaults:
    mode: broadcast
    encrypt: true
    sign: false
    ack: true
    retry_max: 3
    ttl: 3600
  transports:
    syncthing:
      enabled: true
      priority: 1
    file:
      enabled: true
      priority: 2
      settings:
        inbox_path: /tmp/inbox
    ssh:
      enabled: false
      priority: 3
"""
        )

        config = SKCommConfig.from_yaml(config_file)
        assert config.identity.name == "opus"
        assert config.identity.fingerprint == "ABCD1234"
        assert config.default_mode == RoutingMode.BROADCAST
        assert config.sign is False
        assert config.retry_max == 3
        assert config.ttl == 3600
        assert len(config.transports) == 3
        assert config.transports["syncthing"].enabled is True
        assert config.transports["syncthing"].priority == 1
        assert config.transports["ssh"].enabled is False
        assert config.transports["file"].settings["inbox_path"] == "/tmp/inbox"

    def test_config_from_missing_file(self, tmp_path):
        """Edge case: missing file returns defaults."""
        config = SKCommConfig.from_yaml(tmp_path / "nonexistent.yml")
        assert config.identity.name == "unknown"
        assert config.default_mode == RoutingMode.FAILOVER

    def test_config_from_empty_yaml(self, tmp_path):
        """Edge case: empty YAML file returns defaults."""
        config_file = tmp_path / "empty.yml"
        config_file.write_text("")
        config = SKCommConfig.from_yaml(config_file)
        assert config.identity.name == "unknown"

    def test_config_from_invalid_yaml(self, tmp_path):
        """Edge case: malformed YAML returns defaults gracefully."""
        config_file = tmp_path / "bad.yml"
        config_file.write_text(": : : not valid yaml [[[")
        config = SKCommConfig.from_yaml(config_file)
        assert config.identity.name == "unknown"

    def test_config_boolean_transport(self, tmp_path):
        """Edge case: transport config as boolean (shorthand)."""
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            """
skcomm:
  transports:
    file: true
    ssh: false
"""
        )
        config = SKCommConfig.from_yaml(config_file)
        assert config.transports["file"].enabled is True
        assert config.transports["ssh"].enabled is False


class TestLoadConfig:
    """Tests for the load_config convenience function."""

    def test_load_nonexistent(self, tmp_path):
        """Edge case: load from nonexistent path returns defaults."""
        config = load_config(str(tmp_path / "nope.yml"))
        assert config.identity.name == "unknown"
