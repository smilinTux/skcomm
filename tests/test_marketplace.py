"""Tests for the SKWorld marketplace — skill publishing and discovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from skcomm.marketplace import (
    DEFAULT_RELAYS,
    NOSTR_SKILL_KIND,
    NOSTR_SKILL_PREFIX,
    SkillManifest,
    SkillRegistry,
    publish_skill,
    search_skills,
)


def _sample_manifest(**overrides) -> SkillManifest:
    """Create a sample skill manifest for testing."""
    defaults = {
        "name": "test-skill",
        "title": "Test Skill",
        "version": "1.0.0",
        "author": "test-agent",
        "description": "A skill for testing.",
        "tags": ["testing", "demo"],
        "license": "MIT",
    }
    defaults.update(overrides)
    return SkillManifest(**defaults)


# ═══════════════════════════════════════════════════════════
# SkillManifest model
# ═══════════════════════════════════════════════════════════


class TestSkillManifest:
    """Test the SkillManifest pydantic model."""

    def test_basic_creation(self):
        m = SkillManifest(name="hello", title="Hello Skill")
        assert m.name == "hello"
        assert m.version == "0.1.0"
        assert m.license == "Apache-2.0"

    def test_full_manifest(self):
        m = _sample_manifest(repo="https://github.com/test/skill")
        assert m.author == "test-agent"
        assert m.repo == "https://github.com/test/skill"
        assert "testing" in m.tags

    def test_to_yaml(self):
        m = _sample_manifest()
        yml = m.to_yaml()
        parsed = yaml.safe_load(yml)
        assert parsed["name"] == "test-skill"
        assert parsed["version"] == "1.0.0"

    def test_from_yaml_file(self, tmp_path):
        path = tmp_path / "skill.yml"
        path.write_text(yaml.dump({
            "name": "from-file",
            "title": "From File",
            "version": "2.0.0",
            "tags": ["file"],
        }))
        m = SkillManifest.from_yaml_file(path)
        assert m.name == "from-file"
        assert m.version == "2.0.0"

    def test_from_yaml_file_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SkillManifest.from_yaml_file(tmp_path / "nope.yml")

    def test_from_yaml_file_invalid_raises(self, tmp_path):
        path = tmp_path / "bad.yml"
        path.write_text("just a string")
        with pytest.raises(ValueError):
            SkillManifest.from_yaml_file(path)

    def test_json_roundtrip(self):
        m = _sample_manifest()
        data = m.model_dump_json()
        loaded = SkillManifest.model_validate_json(data)
        assert loaded.name == m.name
        assert loaded.tags == m.tags


# ═══════════════════════════════════════════════════════════
# SkillRegistry (local persistence)
# ═══════════════════════════════════════════════════════════


class TestSkillRegistry:
    """Test the local skill registry."""

    def test_install_and_get(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        reg.install(_sample_manifest())
        loaded = reg.get("test-skill")
        assert loaded is not None
        assert loaded.name == "test-skill"
        assert loaded.version == "1.0.0"

    def test_get_nonexistent(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        assert reg.get("nope") is None

    def test_list_all(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        reg.install(_sample_manifest(name="alpha", title="Alpha"))
        reg.install(_sample_manifest(name="beta", title="Beta"))
        reg.install(_sample_manifest(name="gamma", title="Gamma"))
        skills = reg.list_all()
        assert len(skills) == 3
        assert [s.name for s in skills] == ["alpha", "beta", "gamma"]

    def test_list_empty(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        assert reg.list_all() == []

    def test_remove(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        reg.install(_sample_manifest())
        assert reg.remove("test-skill") is True
        assert reg.get("test-skill") is None

    def test_remove_nonexistent(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        assert reg.remove("ghost") is False

    def test_install_overwrites(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        reg.install(_sample_manifest(version="1.0.0"))
        reg.install(_sample_manifest(version="2.0.0"))
        loaded = reg.get("test-skill")
        assert loaded.version == "2.0.0"

    def test_yaml_file_is_valid(self, tmp_path):
        reg = SkillRegistry(skills_dir=tmp_path / "skills")
        reg.install(_sample_manifest())
        path = tmp_path / "skills" / "test-skill.yml"
        assert path.exists()
        raw = yaml.safe_load(path.read_text())
        assert raw["name"] == "test-skill"

    def test_creates_dir_if_missing(self, tmp_path):
        deep = tmp_path / "a" / "b" / "skills"
        reg = SkillRegistry(skills_dir=deep)
        reg.install(_sample_manifest())
        assert deep.exists()


# ═══════════════════════════════════════════════════════════
# Nostr publish (mocked relay)
# ═══════════════════════════════════════════════════════════


class TestPublishSkill:
    """Test skill publishing to Nostr relays."""

    @pytest.fixture(autouse=True)
    def _check_nostr(self):
        """Skip if Nostr crypto deps unavailable."""
        pytest.importorskip("cryptography", reason="Nostr deps required")

    def _make_key(self) -> str:
        from skcomm.transports.nostr import _random_secret

        return _random_secret().hex()

    def test_publish_success(self):
        manifest = _sample_manifest()
        key = self._make_key()

        captured: list[dict] = []

        def mock_publish(relay_url, event, timeout=5.0):
            captured.append(event)
            return True

        with patch("skcomm.transports.nostr._publish_to_relay", mock_publish):
            event_id = publish_skill(manifest, key)

        assert event_id is not None
        assert len(event_id) == 64
        assert len(captured) == 1

        event = captured[0]
        assert event["kind"] == NOSTR_SKILL_KIND
        d_tags = [t[1] for t in event["tags"] if t[0] == "d"]
        assert any(f"{NOSTR_SKILL_PREFIX}:test-skill" in d for d in d_tags)

        content = json.loads(event["content"])
        assert content["name"] == "test-skill"

    def test_publish_includes_tags(self):
        manifest = _sample_manifest(tags=["security", "email"])
        key = self._make_key()

        captured: list[dict] = []

        def mock_publish(relay_url, event, timeout=5.0):
            captured.append(event)
            return True

        with patch("skcomm.transports.nostr._publish_to_relay", mock_publish):
            publish_skill(manifest, key)

        t_tags = [t[1] for t in captured[0]["tags"] if t[0] == "t"]
        assert "security" in t_tags
        assert "email" in t_tags

    def test_publish_all_relays_fail(self):
        manifest = _sample_manifest()
        key = self._make_key()

        with patch("skcomm.transports.nostr._publish_to_relay", return_value=False):
            result = publish_skill(manifest, key)

        assert result is None

    def test_publish_sets_publisher_pubkey(self):
        manifest = _sample_manifest()
        key = self._make_key()

        captured: list[dict] = []

        def mock_publish(relay_url, event, timeout=5.0):
            captured.append(event)
            return True

        with patch("skcomm.transports.nostr._publish_to_relay", mock_publish):
            publish_skill(manifest, key)

        content = json.loads(captured[0]["content"])
        assert content.get("publisher_pubkey") is not None
        assert len(content["publisher_pubkey"]) == 64


# ═══════════════════════════════════════════════════════════
# Nostr search (mocked relay)
# ═══════════════════════════════════════════════════════════


class TestSearchSkills:
    """Test skill searching from Nostr relays."""

    @pytest.fixture(autouse=True)
    def _check_nostr(self):
        pytest.importorskip("cryptography", reason="Nostr deps required")

    def _make_skill_event(self, name: str, title: str, tags: list[str] | None = None) -> dict:
        """Create a mock Nostr event for a skill."""
        manifest = _sample_manifest(name=name, title=title, tags=tags or [])
        manifest.published_at = datetime.now(timezone.utc)
        return {
            "id": "a" * 64,
            "pubkey": "b" * 64,
            "kind": NOSTR_SKILL_KIND,
            "tags": [
                ["d", f"{NOSTR_SKILL_PREFIX}:{name}"],
                ["name", name],
            ],
            "content": manifest.model_dump_json(exclude_none=True),
            "created_at": int(datetime.now(timezone.utc).timestamp()),
            "sig": "c" * 128,
        }

    def test_search_returns_results(self):
        event = self._make_skill_event("security-scan", "Security Scanner", ["security"])

        with patch("skcomm.transports.nostr._query_relay", return_value=[event]):
            results = search_skills()

        assert len(results) == 1
        assert results[0].name == "security-scan"

    def test_search_with_query_filters(self):
        ev1 = self._make_skill_event("sec-scan", "Security Scanner", ["security"])
        ev1["id"] = "1" * 64
        ev2 = self._make_skill_event("chat-bot", "Chat Bot", ["chat"])
        ev2["id"] = "2" * 64

        with patch("skcomm.transports.nostr._query_relay", return_value=[ev1, ev2]):
            results = search_skills(query="security")

        assert len(results) == 1
        assert results[0].name == "sec-scan"

    def test_search_empty_results(self):
        with patch("skcomm.transports.nostr._query_relay", return_value=[]):
            results = search_skills(query="nonexistent")

        assert results == []

    def test_search_deduplicates(self):
        event = self._make_skill_event("dup-skill", "Duplicate")

        with patch("skcomm.transports.nostr._query_relay", return_value=[event, event]):
            results = search_skills()

        assert len(results) == 1

    def test_search_skips_invalid_events(self):
        good = self._make_skill_event("good-skill", "Good")
        bad = {
            "id": "d" * 64,
            "pubkey": "e" * 64,
            "kind": NOSTR_SKILL_KIND,
            "tags": [["d", f"{NOSTR_SKILL_PREFIX}:bad"]],
            "content": "not valid json",
            "created_at": 0,
            "sig": "f" * 128,
        }
        bad["id"] = "f" * 64

        with patch("skcomm.transports.nostr._query_relay", return_value=[good, bad]):
            results = search_skills()

        assert len(results) == 1
        assert results[0].name == "good-skill"

    def test_search_filters_non_skworld_events(self):
        non_skill = {
            "id": "g" * 64,
            "pubkey": "h" * 64,
            "kind": NOSTR_SKILL_KIND,
            "tags": [["d", "something-else"]],
            "content": "{}",
            "created_at": 0,
            "sig": "i" * 128,
        }

        with patch("skcomm.transports.nostr._query_relay", return_value=[non_skill]):
            results = search_skills()

        assert results == []
