"""Tests for SKComm envelope compression — gzip and optional zstd."""

from __future__ import annotations

import base64

import pytest

from skcomm.compression import (
    COMPRESSION_HEADER_GZIP,
    COMPRESSION_HEADER_ZSTD,
    DEFAULT_MIN_SIZE,
    CompressionAlgo,
    HAS_ZSTD,
    compress_payload,
    decompress_payload,
)
from skcomm.models import MessageEnvelope, MessagePayload, MessageType


def _make_envelope(content: str = "hello world") -> MessageEnvelope:
    """Create a minimal envelope for testing."""
    return MessageEnvelope(
        sender="opus",
        recipient="lumina",
        payload=MessagePayload(content=content),
    )


def _large_content(size: int = 1000) -> str:
    """Generate a compressible content string of at least `size` bytes."""
    return "The quick brown fox jumps over the lazy dog. " * (size // 40 + 1)


# ═══════════════════════════════════════════════════════════
# Gzip compression
# ═══════════════════════════════════════════════════════════


class TestGzipCompression:
    """Test gzip compression and decompression."""

    def test_compress_large_payload(self):
        """Content above threshold gets compressed."""
        env = _make_envelope(_large_content(500))
        compressed = compress_payload(env)
        assert compressed.payload.compressed is True
        assert compressed.payload.content.startswith(COMPRESSION_HEADER_GZIP)

    def test_skip_small_payload(self):
        """Content below threshold is not compressed."""
        env = _make_envelope("short")
        result = compress_payload(env)
        assert result.payload.compressed is False
        assert result.payload.content == "short"

    def test_skip_already_compressed(self):
        """Already-compressed payloads are returned unchanged."""
        env = _make_envelope(_large_content())
        compressed = compress_payload(env)
        double = compress_payload(compressed)
        assert double.payload.content == compressed.payload.content

    def test_roundtrip(self):
        """Compress then decompress recovers the original content."""
        original = _large_content(1000)
        env = _make_envelope(original)
        compressed = compress_payload(env)
        decompressed = decompress_payload(compressed)
        assert decompressed.payload.content == original
        assert decompressed.payload.compressed is False

    def test_preserves_content_type(self):
        """Compression preserves the content_type field."""
        env = _make_envelope(_large_content())
        env = env.model_copy(update={"payload": MessagePayload(
            content=_large_content(),
            content_type=MessageType.SEED,
        )})
        compressed = compress_payload(env)
        assert compressed.payload.content_type == MessageType.SEED

    def test_preserves_envelope_metadata(self):
        """Compression doesn't alter envelope_id, sender, recipient, etc."""
        env = _make_envelope(_large_content())
        compressed = compress_payload(env)
        assert compressed.envelope_id == env.envelope_id
        assert compressed.sender == env.sender
        assert compressed.recipient == env.recipient

    def test_custom_min_size(self):
        """Custom min_size threshold is respected."""
        content = "x" * 100
        env = _make_envelope(content)
        result = compress_payload(env, min_size=50)
        assert result.payload.compressed is True

    def test_custom_min_size_too_high(self):
        """Content below custom threshold is not compressed."""
        env = _make_envelope("x" * 100)
        result = compress_payload(env, min_size=200)
        assert result.payload.compressed is False

    def test_compressed_content_is_smaller(self):
        """Compressed content is actually smaller than original."""
        content = _large_content(2000)
        env = _make_envelope(content)
        compressed = compress_payload(env)
        original_size = len(content.encode())
        compressed_raw = base64.b64decode(
            compressed.payload.content[len(COMPRESSION_HEADER_GZIP):]
        )
        assert len(compressed_raw) < original_size


# ═══════════════════════════════════════════════════════════
# Decompression
# ═══════════════════════════════════════════════════════════


class TestDecompression:
    """Test decompression edge cases."""

    def test_decompress_uncompressed_is_noop(self):
        """Decompressing an uncompressed envelope returns it unchanged."""
        env = _make_envelope("not compressed")
        result = decompress_payload(env)
        assert result.payload.content == "not compressed"
        assert result.payload.compressed is False

    def test_decompress_unknown_format(self):
        """Unknown compression header returns envelope as-is."""
        env = _make_envelope("unknown:data")
        env = env.model_copy(update={"payload": MessagePayload(
            content="unknown:data", compressed=True,
        )})
        result = decompress_payload(env)
        assert result.payload.content == "unknown:data"
        assert result.payload.compressed is True

    def test_roundtrip_unicode(self):
        """Compression handles unicode content correctly."""
        content = "Pengu Nation! " * 100
        env = _make_envelope(content)
        compressed = compress_payload(env, min_size=10)
        decompressed = decompress_payload(compressed)
        assert decompressed.payload.content == content

    def test_roundtrip_large_json(self):
        """Compression works for large JSON-like content."""
        import json
        data = json.dumps([{"key": f"value-{i}", "data": "x" * 100} for i in range(50)])
        env = _make_envelope(data)
        compressed = compress_payload(env, min_size=10)
        decompressed = decompress_payload(compressed)
        assert decompressed.payload.content == data


# ═══════════════════════════════════════════════════════════
# Zstd compression (optional)
# ═══════════════════════════════════════════════════════════


class TestZstdCompression:
    """Test zstd compression (skipped if not installed)."""

    @pytest.fixture(autouse=True)
    def _check_zstd(self):
        if not HAS_ZSTD:
            pytest.skip("zstandard not installed")

    def test_zstd_compress_and_decompress(self):
        """Zstd round-trip works."""
        content = _large_content(1000)
        env = _make_envelope(content)
        compressed = compress_payload(env, algorithm=CompressionAlgo.ZSTD)
        assert compressed.payload.compressed is True
        assert compressed.payload.content.startswith(COMPRESSION_HEADER_ZSTD)

        decompressed = decompress_payload(compressed)
        assert decompressed.payload.content == content

    def test_zstd_smaller_than_original(self):
        """Zstd produces smaller output than original."""
        content = _large_content(2000)
        env = _make_envelope(content)
        compressed = compress_payload(env, algorithm=CompressionAlgo.ZSTD)
        compressed_raw = base64.b64decode(
            compressed.payload.content[len(COMPRESSION_HEADER_ZSTD):]
        )
        assert len(compressed_raw) < len(content.encode())


# ═══════════════════════════════════════════════════════════
# Zstd fallback
# ═══════════════════════════════════════════════════════════


class TestZstdFallback:
    """Test gzip fallback when zstd is not available."""

    def test_zstd_falls_back_to_gzip(self):
        """If zstd unavailable, falls back to gzip transparently."""
        if HAS_ZSTD:
            pytest.skip("zstd is installed — can't test fallback")

        content = _large_content(500)
        env = _make_envelope(content)
        compressed = compress_payload(env, algorithm=CompressionAlgo.ZSTD)
        assert compressed.payload.content.startswith(COMPRESSION_HEADER_GZIP)
        decompressed = decompress_payload(compressed)
        assert decompressed.payload.content == content


# ═══════════════════════════════════════════════════════════
# CompressionAlgo enum
# ═══════════════════════════════════════════════════════════


class TestCompressionAlgo:
    """Test the CompressionAlgo enum."""

    def test_values(self):
        assert CompressionAlgo.GZIP == "gzip"
        assert CompressionAlgo.ZSTD == "zstd"
        assert CompressionAlgo.NONE == "none"
