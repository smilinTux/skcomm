"""Tests for SKComm envelope PGP signing and verification."""

from __future__ import annotations

import pgpy
import pytest
from pgpy.constants import (
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from skcomm.models import MessageEnvelope, MessagePayload
from skcomm.signing import (
    EnvelopeSigner,
    EnvelopeVerifier,
    SignedEnvelope,
    VerificationResult,
)

PASSPHRASE = "sign-test-2026"


def _keygen(name: str) -> tuple[str, str]:
    """Generate a test PGP keypair."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new(name, email=f"{name.lower()}@test.io")
    key.add_uid(uid, usage={KeyFlags.Sign, KeyFlags.Certify},
                hashes=[HashAlgorithm.SHA256], ciphers=[SymmetricKeyAlgorithm.AES256])
    key.protect(PASSPHRASE, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), str(key.pubkey)


@pytest.fixture(scope="session")
def alice_keys() -> tuple[str, str]:
    """Alice's keypair."""
    return _keygen("Alice")


@pytest.fixture(scope="session")
def bob_keys() -> tuple[str, str]:
    """Bob's keypair."""
    return _keygen("Bob")


@pytest.fixture()
def envelope() -> MessageEnvelope:
    """A test envelope."""
    return MessageEnvelope(
        sender="alice",
        recipient="bob",
        payload=MessagePayload(content="Sovereignty is non-negotiable."),
    )


@pytest.fixture()
def signer(alice_keys: tuple[str, str]) -> EnvelopeSigner:
    """Signer loaded with Alice's key."""
    priv, _ = alice_keys
    return EnvelopeSigner(priv, PASSPHRASE)


@pytest.fixture()
def verifier(alice_keys: tuple[str, str]) -> EnvelopeVerifier:
    """Verifier with Alice's public key registered."""
    _, pub = alice_keys
    v = EnvelopeVerifier()
    v.add_key("alice", pub)
    return v


class TestEnvelopeSigner:
    """Tests for signing envelopes."""

    def test_sign_produces_signature(
        self, signer: EnvelopeSigner, envelope: MessageEnvelope,
    ) -> None:
        """Happy path: signing produces a non-empty signature."""
        signed = signer.sign(envelope)
        assert signed.is_signed
        assert "PGP SIGNATURE" in signed.signature
        assert signed.signer_fingerprint == signer.fingerprint
        assert signed.content_hash != ""

    def test_signer_has_fingerprint(self, signer: EnvelopeSigner) -> None:
        """Signer exposes its PGP fingerprint."""
        assert len(signer.fingerprint) == 40

    def test_signed_envelope_serialization(
        self, signer: EnvelopeSigner, envelope: MessageEnvelope,
    ) -> None:
        """SignedEnvelope survives bytes roundtrip."""
        signed = signer.sign(envelope)
        data = signed.to_bytes()
        loaded = SignedEnvelope.from_bytes(data)
        assert loaded.signer_fingerprint == signed.signer_fingerprint
        assert loaded.envelope.sender == "alice"
        assert loaded.is_signed


class TestEnvelopeVerifier:
    """Tests for verifying signed envelopes."""

    def test_verify_valid_signature(
        self, signer: EnvelopeSigner, verifier: EnvelopeVerifier,
        envelope: MessageEnvelope,
    ) -> None:
        """Happy path: valid signature verifies."""
        signed = signer.sign(envelope)
        result = verifier.verify(signed)
        assert result.valid is True
        assert "valid" in result.reason.lower()

    def test_verify_unsigned_fails(self, verifier: EnvelopeVerifier, envelope: MessageEnvelope) -> None:
        """Unsigned envelope fails verification."""
        unsigned = SignedEnvelope(envelope=envelope)
        result = verifier.verify(unsigned)
        assert result.valid is False
        assert "No signature" in result.reason

    def test_verify_unknown_signer(
        self, signer: EnvelopeSigner, envelope: MessageEnvelope,
    ) -> None:
        """Signature from unknown signer fails."""
        signed = signer.sign(envelope)
        empty_verifier = EnvelopeVerifier()
        result = empty_verifier.verify(signed)
        assert result.valid is False
        assert "Unknown signer" in result.reason

    def test_verify_tampered_envelope(
        self, signer: EnvelopeSigner, verifier: EnvelopeVerifier,
        envelope: MessageEnvelope,
    ) -> None:
        """Tampered envelope fails hash check."""
        signed = signer.sign(envelope)
        signed.envelope = MessageEnvelope(
            sender="alice", recipient="bob",
            payload=MessagePayload(content="TAMPERED!"),
        )
        result = verifier.verify(signed)
        assert result.valid is False
        assert "tampered" in result.reason.lower()

    def test_verify_wrong_key(
        self, signer: EnvelopeSigner, bob_keys: tuple[str, str],
        envelope: MessageEnvelope,
    ) -> None:
        """Signature verified against wrong key fails."""
        signed = signer.sign(envelope)

        _, bob_pub = bob_keys
        wrong_verifier = EnvelopeVerifier()
        wrong_verifier._keys[signed.signer_fingerprint] = bob_pub

        result = wrong_verifier.verify(signed)
        assert result.valid is False


class TestVerifierKeyManagement:
    """Tests for the verifier's keyring."""

    def test_add_key_returns_fingerprint(self, alice_keys: tuple[str, str]) -> None:
        """add_key returns the key's fingerprint."""
        _, pub = alice_keys
        v = EnvelopeVerifier()
        fp = v.add_key("alice", pub)
        assert len(fp) == 40

    def test_has_key(self, alice_keys: tuple[str, str]) -> None:
        """has_key returns True for registered keys."""
        _, pub = alice_keys
        v = EnvelopeVerifier()
        v.add_key("alice", pub)
        assert v.has_key("alice") is True
        assert v.has_key("unknown") is False

    def test_key_count(self, alice_keys: tuple[str, str], bob_keys: tuple[str, str]) -> None:
        """key_count counts unique fingerprints."""
        _, alice_pub = alice_keys
        _, bob_pub = bob_keys
        v = EnvelopeVerifier()
        v.add_key("alice", alice_pub)
        v.add_key("bob", bob_pub)
        assert v.key_count == 2


class TestVerificationResult:
    """Tests for the result model."""

    def test_defaults(self) -> None:
        """Default result is invalid."""
        r = VerificationResult()
        assert r.valid is False

    def test_valid_result(self) -> None:
        """Valid result tracks all fields."""
        r = VerificationResult(valid=True, reason="OK", fingerprint="abc")
        assert r.valid is True
        assert r.fingerprint == "abc"
