"""Tests for SKComm rate limiter — token bucket throttling."""

from __future__ import annotations

import time

import pytest

from skcomm.ratelimit import RateLimitConfig, RateLimiter, TokenBucket


# ═══════════════════════════════════════════════════════════
# TokenBucket
# ═══════════════════════════════════════════════════════════


class TestTokenBucket:
    """Test the token bucket algorithm."""

    def test_starts_full(self):
        b = TokenBucket(capacity=10, refill_rate=1.0)
        assert b.tokens == pytest.approx(10, abs=0.5)

    def test_allow_consumes_token(self):
        b = TokenBucket(capacity=5, refill_rate=0)
        assert b.allow() is True
        assert b.tokens < 5

    def test_deny_when_empty(self):
        b = TokenBucket(capacity=2, refill_rate=0)
        assert b.allow() is True
        assert b.allow() is True
        assert b.allow() is False

    def test_refill_over_time(self):
        b = TokenBucket(capacity=10, refill_rate=100)
        b.allow()
        b.allow()
        time.sleep(0.05)
        assert b.tokens >= 4

    def test_capacity_is_ceiling(self):
        b = TokenBucket(capacity=5, refill_rate=1000)
        time.sleep(0.05)
        assert b.tokens <= 5.1

    def test_wait_time_zero_when_available(self):
        b = TokenBucket(capacity=10, refill_rate=1.0)
        assert b.wait_time() == 0.0

    def test_wait_time_positive_when_empty(self):
        b = TokenBucket(capacity=1, refill_rate=1.0)
        b.allow()
        w = b.wait_time()
        assert w > 0
        assert w <= 1.1

    def test_custom_cost(self):
        b = TokenBucket(capacity=5, refill_rate=0)
        assert b.allow(cost=3) is True
        assert b.allow(cost=3) is False
        assert b.allow(cost=2) is True


# ═══════════════════════════════════════════════════════════
# RateLimitConfig
# ═══════════════════════════════════════════════════════════


class TestRateLimitConfig:
    """Test configuration model."""

    def test_defaults(self):
        c = RateLimitConfig()
        assert c.enabled is True
        assert c.transport_capacity == 30
        assert c.peer_capacity == 10

    def test_disabled(self):
        c = RateLimitConfig(enabled=False)
        assert c.enabled is False


# ═══════════════════════════════════════════════════════════
# RateLimiter — basic allow/deny
# ═══════════════════════════════════════════════════════════


class TestRateLimiterBasics:
    """Test two-tier rate limiting."""

    def test_allow_normal_traffic(self):
        rl = RateLimiter()
        assert rl.allow("syncthing", "lumina") is True

    def test_deny_after_burst(self):
        config = RateLimitConfig(transport_capacity=3, transport_refill=0, peer_capacity=10, peer_refill=0)
        rl = RateLimiter(default_config=config)

        assert rl.allow("nostr", "a") is True
        assert rl.allow("nostr", "b") is True
        assert rl.allow("nostr", "c") is True
        assert rl.allow("nostr", "d") is False

    def test_peer_limit_independent(self):
        config = RateLimitConfig(transport_capacity=100, transport_refill=0, peer_capacity=2, peer_refill=0)
        rl = RateLimiter(default_config=config)

        assert rl.allow("nostr", "lumina") is True
        assert rl.allow("nostr", "lumina") is True
        assert rl.allow("nostr", "lumina") is False
        assert rl.allow("nostr", "opus") is True

    def test_disabled_always_allows(self):
        config = RateLimitConfig(enabled=False)
        rl = RateLimiter(default_config=config)
        for _ in range(100):
            assert rl.allow("nostr", "lumina") is True

    def test_without_peer(self):
        rl = RateLimiter()
        assert rl.allow("file") is True


# ═══════════════════════════════════════════════════════════
# RateLimiter — per-transport overrides
# ═══════════════════════════════════════════════════════════


class TestOverrides:
    """Test per-transport config overrides."""

    def test_override_applies(self):
        default = RateLimitConfig(transport_capacity=100, transport_refill=0)
        nostr_config = RateLimitConfig(transport_capacity=2, transport_refill=0, peer_capacity=10, peer_refill=0)
        rl = RateLimiter(default_config=default, overrides={"nostr": nostr_config})

        assert rl.allow("nostr") is True
        assert rl.allow("nostr") is True
        assert rl.allow("nostr") is False

        for _ in range(50):
            assert rl.allow("syncthing") is True

    def test_override_does_not_affect_others(self):
        nostr_config = RateLimitConfig(transport_capacity=1, transport_refill=0, peer_capacity=10, peer_refill=0)
        rl = RateLimiter(overrides={"nostr": nostr_config})

        rl.allow("nostr")
        assert rl.allow("nostr") is False
        assert rl.allow("file") is True


# ═══════════════════════════════════════════════════════════
# RateLimiter — wait_time and status
# ═══════════════════════════════════════════════════════════


class TestWaitAndStatus:
    """Test wait_time and status reporting."""

    def test_wait_time_zero_initially(self):
        rl = RateLimiter()
        assert rl.wait_time("syncthing") == 0.0

    def test_wait_time_after_exhaustion(self):
        config = RateLimitConfig(transport_capacity=1, transport_refill=1.0, peer_capacity=10, peer_refill=1.0)
        rl = RateLimiter(default_config=config)
        rl.allow("nostr")
        w = rl.wait_time("nostr")
        assert w > 0

    def test_wait_time_disabled(self):
        config = RateLimitConfig(enabled=False)
        rl = RateLimiter(default_config=config)
        assert rl.wait_time("nostr") == 0.0

    def test_status_shows_buckets(self):
        rl = RateLimiter()
        rl.allow("syncthing", "lumina")
        s = rl.status()
        assert "transport:syncthing" in s
        assert "peer:syncthing:lumina" in s
        assert s["transport:syncthing"]["capacity"] == 30

    def test_status_empty_initially(self):
        rl = RateLimiter()
        assert rl.status() == {}


# ═══════════════════════════════════════════════════════════
# Refill behavior
# ═══════════════════════════════════════════════════════════


class TestRefill:
    """Test that tokens refill correctly over time."""

    def test_refill_restores_after_denial(self):
        config = RateLimitConfig(transport_capacity=1, transport_refill=100, peer_capacity=10, peer_refill=100)
        rl = RateLimiter(default_config=config)

        rl.allow("nostr")
        assert rl.allow("nostr") is False

        time.sleep(0.05)
        assert rl.allow("nostr") is True

    def test_peer_refill_independent(self):
        config = RateLimitConfig(transport_capacity=100, transport_refill=100, peer_capacity=1, peer_refill=100)
        rl = RateLimiter(default_config=config)

        rl.allow("nostr", "lumina")
        assert rl.allow("nostr", "lumina") is False

        time.sleep(0.05)
        assert rl.allow("nostr", "lumina") is True
