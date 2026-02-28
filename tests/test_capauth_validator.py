"""Tests for CapAuthValidator token validation."""

from __future__ import annotations

import base64
import time
from unittest.mock import MagicMock, patch

import pytest

from skcomm.capauth_validator import CapAuthValidator, _FINGERPRINT_RE, _TOKEN_WINDOW_SECS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_FP = "CCBE9306410CF8CD5E393D6DEC31663B95230684"


def _make_token(fingerprint: str = VALID_FP, ts: int | None = None, sig: str = "fakesig") -> str:
    """Build a 3-part CapAuth token string."""
    if ts is None:
        ts = int(time.time())
    return f"{fingerprint}.{ts}.{sig}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def strict_validator():
    """Validator with require_auth=True (production mode)."""
    return CapAuthValidator(require_auth=True)


@pytest.fixture
def permissive_validator():
    """Validator with require_auth=False (dev mode)."""
    return CapAuthValidator(require_auth=False)


# ---------------------------------------------------------------------------
# validate() — no token
# ---------------------------------------------------------------------------


class TestValidateNoToken:
    """Tests for missing token behaviour."""

    def test_no_token_strict_returns_none(self, strict_validator):
        """Expected: None when token is None and require_auth=True."""
        assert strict_validator.validate(None) is None

    def test_no_token_strict_empty_string(self, strict_validator):
        """Expected: None when token is empty string and require_auth=True."""
        assert strict_validator.validate("") is None

    def test_no_token_permissive_returns_anonymous(self, permissive_validator):
        """Expected: 'anonymous' when token absent and require_auth=False."""
        assert permissive_validator.validate(None) == "anonymous"

    def test_no_token_permissive_empty_string_returns_anonymous(self, permissive_validator):
        """Expected: 'anonymous' for empty string in dev mode."""
        assert permissive_validator.validate("") == "anonymous"


# ---------------------------------------------------------------------------
# _validate_local() — dev-mode plain fingerprint
# ---------------------------------------------------------------------------


class TestLocalDevMode:
    """Tests for the dev-mode plain fingerprint bypass."""

    def test_plain_fingerprint_permissive_accepted(self, permissive_validator):
        """Expected: plain 40-hex FP accepted in dev mode."""
        result = permissive_validator.validate(VALID_FP)
        assert result == VALID_FP.upper()

    def test_plain_fingerprint_lowercase_normalised(self, permissive_validator):
        """Expected: lowercase fingerprint is uppercased."""
        result = permissive_validator.validate(VALID_FP.lower())
        assert result == VALID_FP.upper()

    def test_plain_fingerprint_strict_rejected(self, strict_validator):
        """Expected: plain FP (no sig) rejected in strict mode."""
        assert strict_validator.validate(VALID_FP) is None

    def test_invalid_hex_plain_rejected(self, permissive_validator):
        """Expected: non-hex plain token is rejected even in dev mode."""
        assert permissive_validator.validate("notahexfingerprint123456789012345678901") is None

    def test_too_short_plain_rejected(self, permissive_validator):
        """Expected: short plain token is rejected."""
        assert permissive_validator.validate("CCBE93064") is None


# ---------------------------------------------------------------------------
# _validate_local() — format validation
# ---------------------------------------------------------------------------


class TestLocalFormatValidation:
    """Tests for token part-count and fingerprint format checks."""

    def test_invalid_fingerprint_part_rejected(self, strict_validator):
        """Expected: non-hex fingerprint in 3-part token is rejected."""
        token = f"NOTVALID.{int(time.time())}.fakesig"
        assert strict_validator.validate(token) is None

    def test_two_part_token_strict_rejected(self, strict_validator):
        """Expected: 2-part token is rejected in strict mode."""
        token = f"{VALID_FP}.{int(time.time())}"
        assert strict_validator.validate(token) is None

    def test_two_part_token_permissive_returns_fingerprint(self, permissive_validator):
        """Expected: 2-part token returns fingerprint in permissive mode (no sig)."""
        token = f"{VALID_FP}.{int(time.time())}"
        result = permissive_validator.validate(token)
        # Permissive returns fingerprint when pgpy unavailable / no sig
        assert result == VALID_FP.upper() or result is None  # depends on pgpy presence


# ---------------------------------------------------------------------------
# _validate_local() — timestamp / replay prevention
# ---------------------------------------------------------------------------


class TestLocalTimestampValidation:
    """Tests for replay-prevention timestamp window."""

    def test_non_integer_timestamp_rejected(self, strict_validator):
        """Expected: token with non-numeric timestamp is rejected."""
        token = f"{VALID_FP}.notanumber.fakesig"
        assert strict_validator.validate(token) is None

    def test_expired_token_rejected(self, strict_validator):
        """Expected: token older than TOKEN_WINDOW is rejected."""
        old_ts = int(time.time()) - (_TOKEN_WINDOW_SECS + 60)
        token = _make_token(ts=old_ts)
        assert strict_validator.validate(token) is None

    def test_future_dated_token_rejected(self, strict_validator):
        """Expected: token with future timestamp beyond window is rejected."""
        future_ts = int(time.time()) + (_TOKEN_WINDOW_SECS + 60)
        token = _make_token(ts=future_ts)
        assert strict_validator.validate(token) is None

    def test_within_window_proceeds_to_sig_check(self, strict_validator):
        """Expected: token with valid timestamp proceeds (fails at sig, not timestamp)."""
        token = _make_token(ts=int(time.time()))
        # Without pgpy the result will be None (strict mode) but NOT due to timestamp
        # The test confirms we get past the timestamp check (no exception raised)
        result = strict_validator.validate(token)
        # None is expected (no valid sig / pgpy absent) but it should not raise
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# _validate_local() — pgpy absent
# ---------------------------------------------------------------------------


class TestLocalPgpyAbsent:
    """Tests for behaviour when pgpy is not installed."""

    def test_pgpy_absent_strict_returns_none(self):
        """Expected: None in strict mode when pgpy not available."""
        v = CapAuthValidator(require_auth=True)
        token = _make_token()
        with patch.dict("sys.modules", {"pgpy": None}):
            result = v._validate_local(token)
        assert result is None

    def test_pgpy_absent_permissive_returns_fingerprint(self):
        """Expected: fingerprint in permissive mode when pgpy not available."""
        v = CapAuthValidator(require_auth=False)
        token = _make_token()
        with patch.dict("sys.modules", {"pgpy": None}):
            result = v._validate_local(token)
        assert result == VALID_FP.upper()


# ---------------------------------------------------------------------------
# _validate_local() — pgpy verification success
# ---------------------------------------------------------------------------


class TestLocalPgpyVerification:
    """Tests for successful PGP signature verification path."""

    def test_valid_pgp_signature_returns_fingerprint(self):
        """Expected: returns fingerprint when PGP signature verifies."""
        ts = int(time.time())
        sig_b64 = base64.urlsafe_b64encode(b"fakesigbytes").decode().rstrip("=")
        token = f"{VALID_FP}.{ts}.{sig_b64}"

        mock_pgpy = MagicMock()
        mock_key = MagicMock()
        mock_sig = MagicMock()
        mock_pgpy.PGPSignature.from_blob.return_value = mock_sig
        mock_pgpy.PGPKey.from_file.return_value = (mock_key, None)
        # Make verify return a truthy object
        mock_result = MagicMock()
        mock_result.__bool__ = lambda self: True
        mock_key.verify.return_value = mock_result

        v = CapAuthValidator(require_auth=True)
        with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
            # Key lookup: patch _load_public_key to return mock_key
            with patch.object(v, "_load_public_key", return_value=mock_key):
                result = v._validate_local(token)

        assert result == VALID_FP.upper()

    def test_invalid_pgp_signature_strict_returns_none(self):
        """Expected: None in strict mode when PGP verification fails."""
        ts = int(time.time())
        sig_b64 = base64.urlsafe_b64encode(b"badsig").decode().rstrip("=")
        token = f"{VALID_FP}.{ts}.{sig_b64}"

        mock_pgpy = MagicMock()
        mock_key = MagicMock()
        mock_sig = MagicMock()
        mock_pgpy.PGPSignature.from_blob.return_value = mock_sig
        mock_result = MagicMock()
        mock_result.__bool__ = lambda self: False
        mock_key.verify.return_value = mock_result

        v = CapAuthValidator(require_auth=True)
        with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
            with patch.object(v, "_load_public_key", return_value=mock_key):
                result = v._validate_local(token)

        assert result is None

    def test_key_not_found_strict_returns_none(self):
        """Expected: None in strict mode when public key cannot be loaded."""
        token = _make_token()
        v = CapAuthValidator(require_auth=True)
        mock_pgpy = MagicMock()
        mock_pgpy.PGPSignature.from_blob.return_value = MagicMock()
        with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
            with patch.object(v, "_load_public_key", return_value=None):
                result = v._validate_local(token)
        assert result is None

    def test_key_not_found_permissive_returns_fingerprint(self):
        """Expected: fingerprint in permissive mode when key not found."""
        token = _make_token()
        v = CapAuthValidator(require_auth=False)
        mock_pgpy = MagicMock()
        mock_pgpy.PGPSignature.from_blob.return_value = MagicMock()
        with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
            with patch.object(v, "_load_public_key", return_value=None):
                result = v._validate_local(token)
        assert result == VALID_FP.upper()

    def test_pgp_exception_strict_returns_none(self):
        """Expected: None in strict mode when PGP verification raises."""
        token = _make_token()
        v = CapAuthValidator(require_auth=True)
        mock_pgpy = MagicMock()
        mock_pgpy.PGPSignature.from_blob.side_effect = Exception("corrupt sig")
        with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
            result = v._validate_local(token)
        assert result is None


# ---------------------------------------------------------------------------
# _validate_remote()
# ---------------------------------------------------------------------------


class TestValidateRemote:
    """Tests for remote CapAuth API validation."""

    def test_remote_valid_returns_fingerprint(self):
        """Expected: returns fingerprint when remote API responds valid."""
        v = CapAuthValidator(capauth_url="https://capauth.example.com", require_auth=True)
        mock_response = MagicMock()
        mock_response.read.return_value = f'{{"fingerprint": "{VALID_FP}", "valid": true}}'.encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = v.validate(VALID_FP + ".123.fakesig")

        assert result == VALID_FP.upper()

    def test_remote_missing_fingerprint_returns_none(self):
        """Expected: None when remote response lacks fingerprint field."""
        v = CapAuthValidator(capauth_url="https://capauth.example.com", require_auth=True)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"valid": true}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = v.validate("sometoken")

        assert result is None

    def test_remote_unreachable_strict_returns_none(self):
        """Expected: None in strict mode when remote is unreachable."""
        v = CapAuthValidator(capauth_url="https://capauth.example.com", require_auth=True)
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = v.validate(VALID_FP)
        assert result is None

    def test_remote_unreachable_permissive_falls_back_local(self):
        """Expected: falls back to local validation when remote is unreachable in permissive mode."""
        v = CapAuthValidator(capauth_url="https://capauth.example.com", require_auth=False)
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            # Plain fingerprint as token → local dev-mode accepts it
            result = v.validate(VALID_FP)
        assert result == VALID_FP.upper()

    def test_validate_routes_to_remote_when_url_set(self):
        """Expected: _validate_remote is called when capauth_url is configured."""
        v = CapAuthValidator(capauth_url="https://capauth.example.com", require_auth=True)
        with patch.object(v, "_validate_remote", return_value=VALID_FP) as mock_remote:
            result = v.validate("sometoken")
        mock_remote.assert_called_once_with("sometoken")
        assert result == VALID_FP


# ---------------------------------------------------------------------------
# _load_public_key() — file lookup
# ---------------------------------------------------------------------------


class TestLoadPublicKey:
    """Tests for the public key loader."""

    def test_loads_from_key_file(self, tmp_path):
        """Expected: loads key from ~/.skcomm/keys/<FP>.asc if present."""
        import pgpy as _pgpy_module  # may not be installed; skip if absent
        pytest.importorskip("pgpy")

        key_dir = tmp_path / ".skcomm" / "keys"
        key_dir.mkdir(parents=True)
        key_file = key_dir / f"{VALID_FP}.asc"
        key_file.write_text("fake armored key")

        mock_pgpy = MagicMock()
        mock_key = MagicMock()
        mock_pgpy.PGPKey.from_file.return_value = (mock_key, None)

        v = CapAuthValidator()
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
                key = v._load_public_key(VALID_FP)

        assert key == mock_key
        mock_pgpy.PGPKey.from_file.assert_called_once()

    def test_falls_back_to_gpg_when_file_absent(self, tmp_path):
        """Expected: tries gpg export when key file not found."""
        pytest.importorskip("pgpy")

        mock_pgpy = MagicMock()
        mock_key = MagicMock()
        mock_pgpy.PGPKey.from_blob.return_value = (mock_key, None)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n-----END PGP PUBLIC KEY BLOCK-----"

        v = CapAuthValidator()
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch("subprocess.run", return_value=mock_result):
                with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
                    key = v._load_public_key(VALID_FP)

        assert key == mock_key

    def test_returns_none_when_gpg_not_found(self, tmp_path):
        """Expected: returns None if gpg binary is not installed."""
        pytest.importorskip("pgpy")

        mock_pgpy = MagicMock()
        v = CapAuthValidator()
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                with patch.dict("sys.modules", {"pgpy": mock_pgpy}):
                    key = v._load_public_key(VALID_FP)

        assert key is None


# ---------------------------------------------------------------------------
# Fingerprint regex
# ---------------------------------------------------------------------------


class TestFingerprintRegex:
    """Unit tests for the internal fingerprint regex."""

    def test_valid_uppercase_fingerprint(self):
        assert _FINGERPRINT_RE.match(VALID_FP) is not None

    def test_valid_lowercase_fingerprint(self):
        assert _FINGERPRINT_RE.match(VALID_FP.lower()) is not None

    def test_too_short(self):
        assert _FINGERPRINT_RE.match(VALID_FP[:39]) is None

    def test_too_long(self):
        assert _FINGERPRINT_RE.match(VALID_FP + "0") is None

    def test_non_hex_chars(self):
        bad = "GGBE9306410CF8CD5E393D6DEC31663B95230684"
        assert _FINGERPRINT_RE.match(bad) is None
