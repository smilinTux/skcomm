"""
SKComm router — the brain that picks how to deliver.

Decides which transport(s) to use based on routing mode,
transport priority, health status, and peer configuration.
Handles failover, broadcast, and retry logic.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .models import MessageEnvelope, RoutingMode, Urgency
from .transport import (
    DeliveryReport,
    SendResult,
    Transport,
    TransportCategory,
    TransportStatus,
)

logger = logging.getLogger("skcomm.router")


class Router:
    """Transport router with multi-mode delivery and automatic failover.

    Supports four routing modes:
    - failover: try transports in priority order, stop on first success
    - broadcast: send via ALL available transports simultaneously
    - stealth: use only high-stealth transports (file, dns_txt, ipfs)
    - speed: use only low-latency transports (netcat, tailscale, iroh)

    Args:
        transports: List of configured Transport instances.
        default_mode: Fallback routing mode when envelope doesn't specify.
    """

    STEALTH_CATEGORIES = {TransportCategory.STEALTH, TransportCategory.FILE_BASED}
    SPEED_CATEGORIES = {TransportCategory.REALTIME}

    def __init__(
        self,
        transports: Optional[list[Transport]] = None,
        default_mode: RoutingMode = RoutingMode.FAILOVER,
    ):
        self._transports: list[Transport] = transports or []
        self._default_mode = default_mode
        self._seen_ids: dict[str, float] = {}
        self._seen_ttl = 7 * 24 * 3600  # 7 days

    @property
    def transports(self) -> list[Transport]:
        """All registered transports, sorted by priority."""
        return sorted(self._transports, key=lambda t: t.priority)

    def register_transport(self, transport: Transport) -> None:
        """Add a transport to the routing table.

        Args:
            transport: A configured Transport instance.
        """
        existing = next((t for t in self._transports if t.name == transport.name), None)
        if existing:
            self._transports.remove(existing)
        self._transports.append(transport)
        logger.info(
            "Registered transport '%s' (priority=%d, category=%s)",
            transport.name,
            transport.priority,
            transport.category.value,
        )

    def unregister_transport(self, name: str) -> bool:
        """Remove a transport from the routing table.

        Args:
            name: Transport name to remove.

        Returns:
            True if the transport was found and removed.
        """
        before = len(self._transports)
        self._transports = [t for t in self._transports if t.name != name]
        removed = len(self._transports) < before
        if removed:
            logger.info("Unregistered transport '%s'", name)
        return removed

    def route(self, envelope: MessageEnvelope) -> DeliveryReport:
        """Route an envelope through the appropriate transport(s).

        Selects transports based on the envelope's routing mode,
        filters by availability, and handles delivery with retry.

        Args:
            envelope: The message envelope to deliver.

        Returns:
            DeliveryReport with all attempt results.
        """
        mode = envelope.routing.mode or self._default_mode
        report = DeliveryReport(envelope_id=envelope.envelope_id, delivered=False)

        candidates = self._select_transports(mode, envelope)
        if not candidates:
            logger.warning(
                "No available transports for envelope %s (mode=%s)",
                envelope.envelope_id[:8],
                mode.value,
            )
            return report

        envelope_bytes = envelope.to_bytes()

        if mode == RoutingMode.BROADCAST:
            report = self._route_broadcast(envelope_bytes, envelope, candidates, report)
        else:
            report = self._route_failover(envelope_bytes, envelope, candidates, report)

        if report.delivered:
            logger.info(
                "Delivered %s via %s",
                envelope.envelope_id[:8],
                report.successful_transport,
            )
        else:
            logger.warning(
                "Failed to deliver %s after %d attempts",
                envelope.envelope_id[:8],
                len(report.attempts),
            )

        return report

    def receive_all(self) -> list[bytes]:
        """Poll all transports for incoming envelopes.

        Returns:
            List of raw envelope bytes from all transports,
            deduplicated by envelope_id.
        """
        self._prune_seen_ids()
        all_data: list[bytes] = []

        for transport in self.transports:
            if not transport.is_available():
                continue
            try:
                incoming = transport.receive()
                for data in incoming:
                    env_id = self._extract_envelope_id(data)
                    if env_id and env_id in self._seen_ids:
                        logger.debug(
                            "Duplicate envelope %s via %s — skipping",
                            env_id[:8],
                            transport.name,
                        )
                        continue
                    if env_id:
                        self._seen_ids[env_id] = time.time()
                    all_data.append(data)
            except Exception:
                logger.exception("Error receiving from transport '%s'", transport.name)

        return all_data

    def health_report(self) -> dict[str, dict]:
        """Get health status of all registered transports.

        Returns:
            Dict mapping transport name to health info.
        """
        report = {}
        for transport in self.transports:
            try:
                health = transport.health_check()
                report[transport.name] = health.model_dump(mode="json")
            except Exception as exc:
                report[transport.name] = {
                    "transport_name": transport.name,
                    "status": "unavailable",
                    "error": str(exc),
                }
        return report

    def _select_transports(
        self, mode: RoutingMode, envelope: MessageEnvelope
    ) -> list[Transport]:
        """Filter and sort transports for the given routing mode.

        Args:
            mode: The routing mode to apply.
            envelope: The envelope being routed (for preferred transport hints).

        Returns:
            Sorted list of eligible, available transports.
        """
        available = [t for t in self._transports if t.is_available()]

        if mode == RoutingMode.STEALTH:
            available = [t for t in available if t.category in self.STEALTH_CATEGORIES]
        elif mode == RoutingMode.SPEED:
            available = [t for t in available if t.category in self.SPEED_CATEGORIES]

        preferred = envelope.routing.preferred_transports
        if preferred:
            # Reason: boost preferred transports to the front while keeping
            # non-preferred as fallbacks in their natural priority order
            preferred_set = set(preferred)
            boosted = [t for t in available if t.name in preferred_set]
            rest = [t for t in available if t.name not in preferred_set]
            return sorted(boosted, key=lambda t: t.priority) + sorted(
                rest, key=lambda t: t.priority
            )

        return sorted(available, key=lambda t: t.priority)

    def _route_failover(
        self,
        envelope_bytes: bytes,
        envelope: MessageEnvelope,
        candidates: list[Transport],
        report: DeliveryReport,
    ) -> DeliveryReport:
        """Try transports in priority order, stop on first success."""
        for transport in candidates:
            result = self._try_send(transport, envelope_bytes, envelope.recipient)
            report.attempts.append(result)
            if result.success:
                report.delivered = True
                break
        return report

    def _route_broadcast(
        self,
        envelope_bytes: bytes,
        envelope: MessageEnvelope,
        candidates: list[Transport],
        report: DeliveryReport,
    ) -> DeliveryReport:
        """Send via ALL available transports simultaneously."""
        for transport in candidates:
            result = self._try_send(transport, envelope_bytes, envelope.recipient)
            report.attempts.append(result)
            if result.success:
                report.delivered = True
        return report

    def _try_send(
        self, transport: Transport, envelope_bytes: bytes, recipient: str
    ) -> SendResult:
        """Attempt to send through a single transport with error handling."""
        start = time.monotonic()
        try:
            result = transport.send(envelope_bytes, recipient)
            return result
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "Transport '%s' failed: %s", transport.name, exc
            )
            return SendResult(
                success=False,
                transport_name=transport.name,
                envelope_id="",
                latency_ms=elapsed,
                error=str(exc),
            )

    def _extract_envelope_id(self, data: bytes) -> Optional[str]:
        """Best-effort extraction of envelope_id from raw bytes for dedup."""
        import json

        try:
            parsed = json.loads(data)
            return parsed.get("envelope_id")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _prune_seen_ids(self) -> None:
        """Remove expired entries from the deduplication cache."""
        now = time.time()
        expired = [eid for eid, ts in self._seen_ids.items() if now - ts > self._seen_ttl]
        for eid in expired:
            del self._seen_ids[eid]
