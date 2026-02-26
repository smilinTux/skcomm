"""Tests for SKComm envelope encryption (skcomm.crypto).

Covers:
- EnvelopeCrypto sign/verify round-trip
- EnvelopeCrypto encrypt/decrypt round-trip
- KeyStore add/get/has operations
- Graceful fallback when PGPy is unavailable
- Integration with MessageEnvelope model
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skcomm.models import MessageEnvelope, MessagePayload, MessageType


def _generate_test_keypair():
    """Generate a PGPy keypair for testing.

    Returns:
        tuple: (private_armor, public_armor, fingerprint).
    """
    import pgpy
    from pgpy.constants import (
        HashAlgorithm,
        KeyFlags,
        PubKeyAlgorithm,
        SymmetricKeyAlgorithm,
    )

    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("Test Agent", email="test@skcapstone.local")
    key.add_uid(uid, usage={
        KeyFlags.Sign,
        KeyFlags.EncryptCommunications,
        KeyFlags.EncryptStorage,
    }, hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256])

    private_armor = str(key)
    public_armor = str(key.pubkey)
    fingerprint = str(key.fingerprint).replace(" ", "")

    return private_armor, public_armor, fingerprint


@pytest.fixture
def keypair():
    """Generate a test PGP keypair."""
    return _generate_test_keypair()


@pytest.fixture
def peer_keypair():
    """Generate a second keypair for the peer."""
    return _generate_test_keypair()


@pytest.fixture
def crypto(keypair):
    """Create an EnvelopeCrypto with the test key."""
    from skcomm.crypto import EnvelopeCrypto

    priv, pub, fp = keypair
    return EnvelopeCrypto(
        private_key_armor=priv,
        passphrase="",
        own_fingerprint=fp,
    )


@pytest.fixture
def sample_envelope():
    """Create a basic test envelope."""
    return MessageEnvelope(
        sender="opus",
        recipient="lumina",
        payload=MessagePayload(
            content="Hello sovereign world!",
            content_type=MessageType.TEXT,
        ),
    )


class TestSignVerifyRoundTrip:
    """Test PGP signing and verification on envelopes."""

    def test_sign_adds_signature(self, crypto, sample_envelope):
        """Signing an envelope populates the signature field."""
        signed = crypto.sign_payload(sample_envelope)

        assert signed.payload.signature is not None
        assert len(signed.payload.signature) > 0
        assert signed.payload.content == sample_envelope.payload.content

    def test_sign_already_signed_is_noop(self, crypto, sample_envelope):
        """Signing an already-signed envelope is a no-op."""
        signed = crypto.sign_payload(sample_envelope)
        double_signed = crypto.sign_payload(signed)

        assert double_signed.payload.signature == signed.payload.signature

    def test_verify_valid_signature(self, crypto, keypair, sample_envelope):
        """Valid signature verifies against the signer's public key."""
        _, pub_armor, _ = keypair
        signed = crypto.sign_payload(sample_envelope)

        assert crypto.verify_signature(signed, pub_armor)

    def test_verify_wrong_key_fails(self, crypto, peer_keypair, sample_envelope):
        """Signature from one key fails verification with a different key."""
        _, wrong_pub, _ = peer_keypair
        signed = crypto.sign_payload(sample_envelope)

        assert not crypto.verify_signature(signed, wrong_pub)

    def test_verify_no_signature_returns_false(self, crypto, keypair, sample_envelope):
        """Unsigned envelope returns False from verify."""
        _, pub_armor, _ = keypair
        assert not crypto.verify_signature(sample_envelope, pub_armor)


class TestEncryptDecryptRoundTrip:
    """Test PGP encryption and decryption on envelopes."""

    def test_encrypt_decrypt_roundtrip(self, crypto, keypair, sample_envelope):
        """Content survives encrypt -> decrypt with the same keypair."""
        _, pub_armor, _ = keypair
        original_content = sample_envelope.payload.content

        encrypted = crypto.encrypt_payload(sample_envelope, pub_armor)

        assert encrypted.payload.encrypted is True
        assert encrypted.payload.content != original_content
        assert "BEGIN PGP MESSAGE" in encrypted.payload.content

        decrypted = crypto.decrypt_payload(encrypted)

        assert decrypted.payload.encrypted is False
        assert decrypted.payload.content == original_content

    def test_encrypt_already_encrypted_is_noop(self, crypto, keypair, sample_envelope):
        """Encrypting an already-encrypted envelope is a no-op."""
        _, pub_armor, _ = keypair
        encrypted = crypto.encrypt_payload(sample_envelope, pub_armor)
        double_encrypted = crypto.encrypt_payload(encrypted, pub_armor)

        assert double_encrypted.payload.content == encrypted.payload.content

    def test_decrypt_plaintext_is_noop(self, crypto, sample_envelope):
        """Decrypting a plaintext envelope is a no-op."""
        result = crypto.decrypt_payload(sample_envelope)
        assert result.payload.content == sample_envelope.payload.content

    def test_encrypt_with_peer_key_decrypt_with_own(self, peer_keypair, sample_envelope):
        """Encrypt with peer's public key, decrypt with peer's private key."""
        from skcomm.crypto import EnvelopeCrypto

        peer_priv, peer_pub, peer_fp = peer_keypair
        sender_priv, sender_pub, sender_fp = _generate_test_keypair()

        sender_crypto = EnvelopeCrypto(sender_priv, "", sender_fp)
        peer_crypto = EnvelopeCrypto(peer_priv, "", peer_fp)

        encrypted = sender_crypto.encrypt_payload(sample_envelope, peer_pub)
        assert encrypted.payload.encrypted

        decrypted = peer_crypto.decrypt_payload(encrypted)
        assert decrypted.payload.content == "Hello sovereign world!"


class TestKeyStore:
    """Test KeyStore peer key management."""

    def test_add_and_get(self, keypair):
        """Keys can be added and retrieved by name."""
        from skcomm.crypto import KeyStore

        _, pub_armor, _ = keypair
        store = KeyStore()
        store.add_key("opus", pub_armor)

        assert store.has_key("opus")
        assert store.get_public_key("opus") == pub_armor

    def test_unknown_peer_returns_none(self):
        """Unknown peer returns None."""
        from skcomm.crypto import KeyStore

        store = KeyStore()
        assert store.get_public_key("nobody") is None

    def test_known_peers_list(self, keypair, peer_keypair, tmp_path):
        """known_peers returns all added peer names."""
        from skcomm.crypto import KeyStore

        _, pub1, _ = keypair
        _, pub2, _ = peer_keypair

        # Reason: use isolated tmp dir to avoid picking up real ~/.skcomm/peers entries
        store = KeyStore(peers_dir=tmp_path)
        store.add_key("opus", pub1)
        store.add_key("lumina", pub2)

        peers = store.known_peers
        assert "opus" in peers
        assert "lumina" in peers
        assert len(peers) == 2


class TestEnvelopeCryptoFingerprint:
    """Test fingerprint property."""

    def test_fingerprint_set(self, crypto, keypair):
        """Fingerprint matches the provided value."""
        _, _, fp = keypair
        assert crypto.fingerprint == fp

    def test_fingerprint_empty_default(self):
        """Default fingerprint is empty string."""
        from skcomm.crypto import EnvelopeCrypto

        priv, _, _ = _generate_test_keypair()
        ec = EnvelopeCrypto(priv, "")
        assert ec.fingerprint == ""


class TestGracefulFallback:
    """Test behavior when PGPy is unavailable."""

    def test_sign_without_pgpy_returns_unchanged(self, sample_envelope):
        """Sign returns the envelope unchanged if PGPy is missing."""
        from skcomm.crypto import EnvelopeCrypto

        priv, _, fp = _generate_test_keypair()
        ec = EnvelopeCrypto(priv, "", fp)
        ec._pgp_available = False

        result = ec.sign_payload(sample_envelope)
        assert result.payload.signature is None

    def test_encrypt_without_pgpy_returns_unchanged(self, keypair, sample_envelope):
        """Encrypt returns the envelope unchanged if PGPy is missing."""
        from skcomm.crypto import EnvelopeCrypto

        priv, pub, fp = keypair
        ec = EnvelopeCrypto(priv, "", fp)
        ec._pgp_available = False

        result = ec.encrypt_payload(sample_envelope, pub)
        assert not result.payload.encrypted
