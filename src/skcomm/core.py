"""
SKComm — the sovereign communication engine.

High-level interface that wraps the router, transports, and
envelope creation into a clean send/receive API.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Optional

from .config import SKCommConfig, load_config
from .discovery import PeerStore
from .models import (
    MessageEnvelope,
    MessageMetadata,
    MessagePayload,
    MessageType,
    RoutingConfig,
    RoutingMode,
    Urgency,
)
from .router import Router
from .transport import DeliveryReport, Transport

logger = logging.getLogger("skcomm.core")

# Mapping of transport name to module path within skcomm.transports
BUILTIN_TRANSPORTS: dict[str, str] = {
    "file": "skcomm.transports.file",
    "syncthing": "skcomm.transports.syncthing",
    "nostr": "skcomm.transports.nostr",
    "websocket": "skcomm.transports.websocket",
}


class SKComm:
    """The sovereign communication engine.

    Wraps envelope creation, transport routing, and message
    reception into a simple API. Optionally encrypts and signs
    all outbound envelopes via CapAuth PGP keys.

    Usage:
        comm = SKComm.from_config("~/.skcomm/config.yml")
        comm.send("lumina", "Hello from Opus")
        messages = comm.receive()

    Args:
        config: SKCommConfig instance with all settings.
        router: Optional pre-configured Router.
        crypto: Optional EnvelopeCrypto for PGP encrypt/sign.
        keystore: Optional KeyStore for peer public keys.
    """

    def __init__(
        self,
        config: Optional[SKCommConfig] = None,
        router: Optional[Router] = None,
        crypto: Optional["EnvelopeCrypto"] = None,
        keystore: Optional["KeyStore"] = None,
    ):
        self._config = config or SKCommConfig()
        self._router = router or Router(default_mode=self._config.default_mode)
        self._identity = self._config.identity.name
        self._crypto = crypto
        self._keystore = keystore
        self._ack_tracker = None
        if self._config.ack:
            from .ack import AckTracker
            self._ack_tracker = AckTracker()

    @classmethod
    def from_config(cls, config_path: Optional[str] = None) -> SKComm:
        """Create an SKComm instance from a YAML config file.

        Loads the config, discovers and registers configured transports.
        Auto-initializes CapAuth encryption if keys are available and
        config enables encrypt/sign.

        Args:
            config_path: Path to config file. Defaults to ~/.skcomm/config.yml.

        Returns:
            Configured SKComm instance ready to send and receive.
        """
        config = load_config(config_path)
        router = Router(default_mode=config.default_mode)

        for name, tconf in config.transports.items():
            if not tconf.enabled:
                continue
            transport = _load_transport(name, tconf.priority, tconf.settings)
            if transport:
                router.register_transport(transport)

        crypto = None
        keystore = None
        if config.encrypt or config.sign:
            crypto, keystore = _init_crypto()

        instance = cls(config=config, router=router, crypto=crypto, keystore=keystore)
        crypto_status = "enabled" if crypto else "disabled"
        logger.info(
            "SKComm initialized as '%s' with %d transports, crypto %s",
            config.identity.name,
            len(router.transports),
            crypto_status,
        )
        return instance

    @property
    def identity(self) -> str:
        """This agent's name/identifier."""
        return self._identity

    @property
    def router(self) -> Router:
        """The underlying Router instance."""
        return self._router

    def register_transport(self, transport: Transport) -> None:
        """Register an additional transport at runtime.

        Args:
            transport: A configured Transport instance.
        """
        self._router.register_transport(transport)

    def send(
        self,
        recipient: str,
        message: str,
        *,
        message_type: MessageType = MessageType.TEXT,
        mode: Optional[RoutingMode] = None,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        urgency: Urgency = Urgency.NORMAL,
    ) -> DeliveryReport:
        """Send a message to a recipient.

        Creates an envelope, routes it through available transports.

        Args:
            recipient: Agent name or PGP fingerprint of the recipient.
            message: The message content (plaintext).
            message_type: Type of content being sent.
            mode: Override the default routing mode.
            thread_id: Optional conversation thread ID.
            in_reply_to: Optional envelope_id this is a reply to.
            urgency: Message urgency level.

        Returns:
            DeliveryReport with attempt results.
        """
        preferred_transports = self._resolve_peer_transports(recipient)

        envelope = MessageEnvelope(
            sender=self._identity,
            recipient=recipient,
            payload=MessagePayload(
                content=message,
                content_type=message_type,
            ),
            routing=RoutingConfig(
                mode=mode or self._config.default_mode,
                retry_max=self._config.retry_max,
                retry_backoff=self._config.retry_backoff,
                ttl=self._config.ttl,
                ack_requested=self._config.ack,
                preferred_transports=preferred_transports,
            ),
            metadata=MessageMetadata(
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                urgency=urgency,
            ),
        )

        envelope = self._apply_compression(envelope)
        envelope = self._apply_outbound_crypto(envelope)

        logger.info(
            "Sending %s to %s [%s] via %s (compressed=%s, encrypted=%s, signed=%s)",
            message_type.value,
            recipient,
            envelope.envelope_id[:8],
            (mode or self._config.default_mode).value,
            envelope.payload.compressed,
            envelope.payload.encrypted,
            bool(envelope.payload.signature),
        )

        report = self._router.route(envelope)

        if report.delivered and self._ack_tracker:
            self._ack_tracker.track(envelope)

        return report

    def _resolve_peer_transports(self, recipient: str) -> list[str]:
        """Look up the preferred transports for a recipient from the peer store.

        Checks ~/.skcomm/peers/<name>.yml for a list of configured transports.
        Returns transport names the router should prefer for this recipient.

        Args:
            recipient: Agent name or fingerprint to resolve.

        Returns:
            list[str]: Preferred transport names (may be empty).
        """
        try:
            store = PeerStore()
            peer = store.get(recipient)
            if peer and peer.transports:
                return [t.transport for t in peer.transports]
        except Exception as exc:
            logger.debug("Peer store lookup failed for '%s': %s", recipient, exc)
        return []

    def send_envelope(self, envelope: MessageEnvelope) -> DeliveryReport:
        """Send a pre-built envelope directly.

        Useful for forwarding, ACKs, or envelopes built externally.

        Args:
            envelope: A fully constructed MessageEnvelope.

        Returns:
            DeliveryReport with attempt results.
        """
        return self._router.route(envelope)

    def receive(self) -> list[MessageEnvelope]:
        """Check all transports for incoming messages.

        Polls every available transport, deduplicates, and deserializes.

        Returns:
            List of received MessageEnvelope objects.
        """
        raw_messages = self._router.receive_all()
        envelopes: list[MessageEnvelope] = []

        for data in raw_messages:
            try:
                envelope = MessageEnvelope.from_bytes(data)
                if envelope.is_expired:
                    logger.debug(
                        "Discarding expired envelope %s", envelope.envelope_id[:8]
                    )
                    continue
                envelope = self._apply_inbound_crypto(envelope)
                envelope = self._apply_decompression(envelope)

                if envelope.is_ack and self._ack_tracker:
                    self._ack_tracker.process_ack(envelope)

                self._send_auto_ack(envelope)
                envelopes.append(envelope)
            except Exception:
                logger.warning("Failed to deserialize incoming envelope — skipping")

        logger.info("Received %d message(s)", len(envelopes))
        return envelopes

    def _apply_outbound_crypto(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """Encrypt and/or sign an outbound envelope if crypto is available.

        Args:
            envelope: The envelope to protect.

        Returns:
            MessageEnvelope: Possibly encrypted/signed copy.
        """
        if not self._crypto:
            return envelope

        if self._config.sign and not envelope.payload.signature:
            envelope = self._crypto.sign_payload(envelope)

        if self._config.encrypt and not envelope.payload.encrypted:
            if self._keystore and self._keystore.has_key(envelope.recipient):
                pub_armor = self._keystore.get_public_key(envelope.recipient)
                if pub_armor:
                    envelope = self._crypto.encrypt_payload(envelope, pub_armor)

        return envelope

    def _apply_inbound_crypto(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """Decrypt an inbound envelope if it's encrypted.

        Args:
            envelope: The received envelope.

        Returns:
            MessageEnvelope: Decrypted copy if encrypted, otherwise unchanged.
        """
        if not self._crypto:
            return envelope

        if envelope.payload.encrypted:
            envelope = self._crypto.decrypt_payload(envelope)

        return envelope

    def _send_auto_ack(self, envelope: MessageEnvelope) -> None:
        """Automatically send an ACK for messages that request one.

        Args:
            envelope: The received envelope to potentially acknowledge.
        """
        from .ack import should_ack

        if not should_ack(envelope):
            return

        ack = envelope.make_ack(self._identity)
        try:
            self._router.route(ack)
            logger.debug("Sent auto-ACK for %s to %s", envelope.envelope_id[:8], envelope.sender)
        except Exception as exc:
            logger.warning("Failed to send auto-ACK for %s: %s", envelope.envelope_id[:8], exc)

    @staticmethod
    def _apply_compression(envelope: MessageEnvelope) -> MessageEnvelope:
        """Compress an outbound envelope's payload if worthwhile.

        Args:
            envelope: The envelope to compress.

        Returns:
            MessageEnvelope with compressed content, or unchanged if too small.
        """
        from .compression import compress_payload
        return compress_payload(envelope)

    @staticmethod
    def _apply_decompression(envelope: MessageEnvelope) -> MessageEnvelope:
        """Decompress an inbound envelope's payload if compressed.

        Args:
            envelope: The received envelope.

        Returns:
            MessageEnvelope with decompressed content, or unchanged.
        """
        from .compression import decompress_payload
        return decompress_payload(envelope)

    def status(self) -> dict:
        """Get the current status of SKComm.

        Returns:
            Dict with identity, transport health, crypto state, and config summary.
        """
        crypto_info = {
            "available": self._crypto is not None,
            "encrypt_enabled": self._config.encrypt,
            "sign_enabled": self._config.sign,
            "fingerprint": self._crypto.fingerprint if self._crypto else None,
            "known_peers": self._keystore.known_peers if self._keystore else [],
        }

        return {
            "version": self._config.version,
            "identity": self._config.identity.model_dump(),
            "default_mode": self._config.default_mode.value,
            "transports": self._router.health_report(),
            "transport_count": len(self._router.transports),
            "encrypt": self._config.encrypt,
            "sign": self._config.sign,
            "crypto": crypto_info,
        }


def _init_crypto():
    """Initialize CapAuth-based encryption from the local profile.

    Returns:
        tuple: (EnvelopeCrypto or None, KeyStore or None).
    """
    try:
        from .crypto import EnvelopeCrypto, KeyStore

        crypto = EnvelopeCrypto.from_capauth()
        keystore = KeyStore()
        return crypto, keystore
    except ImportError:
        logger.debug("skcomm.crypto not available")
        return None, None
    except Exception as exc:
        logger.debug("Crypto init failed: %s", exc)
        return None, None


def _load_transport(
    name: str, priority: int, settings: dict
) -> Optional[Transport]:
    """Attempt to load and configure a transport by name.

    Args:
        name: Transport name (e.g., "syncthing", "file").
        priority: Transport priority for routing.
        settings: Transport-specific configuration dict.

    Returns:
        Configured Transport instance, or None on failure.
    """
    module_path = BUILTIN_TRANSPORTS.get(name)
    if not module_path:
        logger.warning("Unknown transport '%s' — skipping", name)
        return None

    try:
        module = importlib.import_module(module_path)
        transport_cls = getattr(module, "create_transport", None)
        if transport_cls is None:
            logger.warning("Transport module '%s' has no create_transport() — skipping", name)
            return None
        transport = transport_cls(priority=priority, **settings)
        return transport
    except ImportError:
        logger.debug("Transport '%s' not yet implemented — skipping", name)
        return None
    except Exception:
        logger.exception("Failed to load transport '%s'", name)
        return None
