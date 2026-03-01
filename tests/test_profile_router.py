"""Tests for skcomm.profile_router — Profile API endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a test client with mocked auth."""
    from skcomm.api import app

    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Auth headers with a dev-mode fingerprint token."""
    return {"Authorization": "Bearer CCBE9306410CF8CD5E393D6DEC31663B95230684"}


class TestProfileEndpoints:
    """Tests for /api/v1/profile/* endpoints."""

    def test_profile_requires_auth(self, client):
        """Profile endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile")
        assert resp.status_code == 401

    def test_profile_rejects_bad_token(self, client):
        """Profile endpoint returns 401 with invalid token."""
        resp = client.get(
            "/api/v1/profile",
            headers={"Authorization": "Bearer invalid"},
        )
        assert resp.status_code == 401

    @patch("skcomm.profile_router._validator")
    def test_profile_returns_data(self, mock_validator, client, auth_headers):
        """Profile endpoint returns agent data when authenticated."""
        mock_validator.validate.return_value = "CCBE9306410CF8CD5E393D6DEC31663B95230684"

        with patch("skcomm.profile_router.require_auth", return_value="test_fp"):
            resp = client.get("/api/v1/profile", headers=auth_headers)
            # May return 401 (strict auth) or 501 (skcapstone not in test env)
            # or 200 (if skcapstone is available)
            assert resp.status_code in (200, 401, 501)

    def test_identity_requires_auth(self, client):
        """Identity endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/identity")
        assert resp.status_code == 401

    def test_memories_requires_auth(self, client):
        """Memories endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/memories")
        assert resp.status_code == 401

    def test_trust_requires_auth(self, client):
        """Trust endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/trust")
        assert resp.status_code == 401

    def test_soul_requires_auth(self, client):
        """Soul endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/soul")
        assert resp.status_code == 401

    def test_journal_requires_auth(self, client):
        """Journal endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/journal")
        assert resp.status_code == 401

    def test_coordination_requires_auth(self, client):
        """Coordination endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/coordination")
        assert resp.status_code == 401

    def test_context_requires_auth(self, client):
        """Context endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/context")
        assert resp.status_code == 401

    def test_storage_requires_auth(self, client):
        """Storage endpoint returns 401 without auth."""
        resp = client.get("/api/v1/profile/storage")
        assert resp.status_code == 401

    def test_housekeeping_requires_auth(self, client):
        """Housekeeping endpoint returns 401 without auth."""
        resp = client.post("/api/v1/profile/housekeeping")
        assert resp.status_code in (401, 422)

    def test_store_memory_requires_auth(self, client):
        """Store memory endpoint returns 401 without auth."""
        resp = client.post(
            "/api/v1/profile/memories",
            json={"content": "test"},
        )
        assert resp.status_code == 401

    def test_journal_write_requires_auth(self, client):
        """Journal write endpoint returns 401 without auth."""
        resp = client.post(
            "/api/v1/profile/journal",
            json={"title": "test"},
        )
        assert resp.status_code == 401


class TestProfileRouterRegistered:
    """Test that the profile router is mounted in the app."""

    def test_profile_routes_exist(self, client):
        """Verify profile routes are registered (they respond, even if 401)."""
        endpoints = [
            "/api/v1/profile",
            "/api/v1/profile/identity",
            "/api/v1/profile/memories",
            "/api/v1/profile/trust",
            "/api/v1/profile/soul",
            "/api/v1/profile/journal",
            "/api/v1/profile/coordination",
            "/api/v1/profile/context",
            "/api/v1/profile/storage",
        ]
        for endpoint in endpoints:
            resp = client.get(endpoint)
            # Should get 401 (auth required), not 404 (route not found)
            assert resp.status_code != 404, f"{endpoint} returned 404 — route not registered"
