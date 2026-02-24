"""
Syncthing transport — file-based P2P messaging over the Syncthing mesh.

Uses the existing Syncthing sync folder (same one used for vault sync)
as a message transport. Envelopes are written as JSON files to per-peer
outbox directories. Syncthing propagates them. The receiver picks up
from their inbox directory.

This is the DEFAULT, always-on transport because Syncthing is already
running for vault sync. No additional infrastructure needed.

Directory layout:
    {comms_root}/
    ├── outbox/
    │   └── {peer}/           # One directory per recipient
    │       └── {id}.skc.json # Envelope files awaiting propagation
    ├── inbox/
    │   └── {peer}/           # One directory per sender
    │       └── {id}.skc.json # Received envelope files
    └── archive/
        └── {id}.skc.json     # Processed envelopes (optional)
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..transport import (
    HealthStatus,
    SendResult,
    Transport,
    TransportCategory,
    TransportStatus,
)

logger = logging.getLogger("skcomm.transports.syncthing")

ENVELOPE_SUFFIX = ".skc.json"
LOCK_SUFFIX = ".skc.lock"


class SyncthingTransport(Transport):
    """File-based transport using Syncthing for P2P propagation.

    Writes envelopes as JSON files to outbox/{peer}/ directories.
    Syncthing detects the new files and syncs them to connected
    devices. The receiver's daemon polls inbox/{peer}/ for new files.

    Attributes:
        name: Always "syncthing".
        priority: Default 1 (highest priority — always-on transport).
        category: FILE_BASED — works offline, no direct network calls.
    """

    name: str = "syncthing"
    priority: int = 1
    category: TransportCategory = TransportCategory.FILE_BASED

    def __init__(
        self,
        comms_root: Optional[Path] = None,
        priority: int = 1,
        archive: bool = True,
        **kwargs,
    ):
        """Initialize the Syncthing transport.

        Args:
            comms_root: Root directory for comms folders. Defaults to
                        ~/.skcapstone/comms/ (same Syncthing share as vault sync).
            priority: Transport priority for routing (lower = higher priority).
            archive: Whether to move processed envelopes to archive/.
        """
        self.priority = priority
        self._archive = archive

        if comms_root is None:
            self._root = Path("~/.skcapstone/comms").expanduser()
        else:
            self._root = Path(comms_root)

        self._outbox = self._root / "outbox"
        self._inbox = self._root / "inbox"
        self._archive_dir = self._root / "archive"

    def configure(self, config: dict) -> None:
        """Load transport-specific configuration.

        Args:
            config: Dict with optional keys: comms_root, archive.
        """
        if "comms_root" in config:
            self._root = Path(config["comms_root"]).expanduser()
            self._outbox = self._root / "outbox"
            self._inbox = self._root / "inbox"
            self._archive_dir = self._root / "archive"

        self._archive = config.get("archive", self._archive)

    def is_available(self) -> bool:
        """Check if the comms directories are accessible.

        Returns:
            True if the outbox and inbox directories exist or can be created.
        """
        try:
            self._ensure_dirs()
            return True
        except OSError:
            return False

    def send(self, envelope_bytes: bytes, recipient: str) -> SendResult:
        """Write an envelope file to the recipient's outbox directory.

        The file is written atomically (write to .tmp then rename) to
        prevent Syncthing from syncing partial files.

        Args:
            envelope_bytes: Serialized MessageEnvelope bytes.
            recipient: Recipient agent name (used as subdirectory name).

        Returns:
            SendResult with success/failure and timing.
        """
        start = time.monotonic()
        envelope_id = self._extract_id(envelope_bytes)

        try:
            peer_outbox = self._outbox / recipient
            peer_outbox.mkdir(parents=True, exist_ok=True)

            filename = f"{envelope_id}{ENVELOPE_SUFFIX}"
            target = peer_outbox / filename
            tmp_target = peer_outbox / f".{filename}.tmp"

            # Reason: atomic write prevents Syncthing from syncing partial files
            tmp_target.write_bytes(envelope_bytes)
            tmp_target.rename(target)

            elapsed = (time.monotonic() - start) * 1000
            logger.info(
                "Wrote envelope %s to %s (%0.1fms)",
                envelope_id[:8],
                target,
                elapsed,
            )

            return SendResult(
                success=True,
                transport_name=self.name,
                envelope_id=envelope_id,
                latency_ms=elapsed,
            )

        except OSError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("Failed to write envelope: %s", exc)
            return SendResult(
                success=False,
                transport_name=self.name,
                envelope_id=envelope_id,
                latency_ms=elapsed,
                error=str(exc),
            )

    def receive(self) -> list[bytes]:
        """Poll all inbox peer directories for new envelopes.

        Reads and removes envelope files from inbox/{peer}/ directories.
        Optionally archives processed files.

        Returns:
            List of raw envelope bytes, one per received file.
        """
        self._ensure_dirs()
        received: list[bytes] = []

        if not self._inbox.exists():
            return received

        for peer_dir in self._inbox.iterdir():
            if not peer_dir.is_dir():
                continue

            for env_file in sorted(peer_dir.glob(f"*{ENVELOPE_SUFFIX}")):
                if env_file.name.startswith("."):
                    continue

                try:
                    data = env_file.read_bytes()
                    received.append(data)

                    if self._archive:
                        self._archive_file(env_file)
                    else:
                        env_file.unlink()

                    logger.debug("Received envelope from %s: %s", peer_dir.name, env_file.name)

                except OSError as exc:
                    logger.warning(
                        "Failed to read envelope %s: %s", env_file, exc
                    )

        return received

    def health_check(self) -> HealthStatus:
        """Check the health of the Syncthing transport.

        Verifies directory accessibility and reports any issues.

        Returns:
            HealthStatus with current state.
        """
        start = time.monotonic()
        details: dict = {}

        try:
            self._ensure_dirs()
            latency = (time.monotonic() - start) * 1000

            outbox_peers = (
                [d.name for d in self._outbox.iterdir() if d.is_dir()]
                if self._outbox.exists()
                else []
            )
            inbox_peers = (
                [d.name for d in self._inbox.iterdir() if d.is_dir()]
                if self._inbox.exists()
                else []
            )
            inbox_count = sum(
                len(list((self._inbox / p).glob(f"*{ENVELOPE_SUFFIX}")))
                for p in inbox_peers
            )

            details = {
                "comms_root": str(self._root),
                "outbox_peers": outbox_peers,
                "inbox_peers": inbox_peers,
                "pending_inbox": inbox_count,
            }

            return HealthStatus(
                transport_name=self.name,
                status=TransportStatus.AVAILABLE,
                latency_ms=latency,
                details=details,
            )

        except OSError as exc:
            latency = (time.monotonic() - start) * 1000
            return HealthStatus(
                transport_name=self.name,
                status=TransportStatus.UNAVAILABLE,
                latency_ms=latency,
                error=str(exc),
                details=details,
            )

    def pending_outbox(self, peer: Optional[str] = None) -> list[Path]:
        """List envelope files waiting in the outbox.

        Args:
            peer: Optional peer name to filter by.

        Returns:
            List of Path objects for pending envelope files.
        """
        if not self._outbox.exists():
            return []

        if peer:
            peer_dir = self._outbox / peer
            if not peer_dir.exists():
                return []
            return sorted(peer_dir.glob(f"*{ENVELOPE_SUFFIX}"))

        files = []
        for peer_dir in self._outbox.iterdir():
            if peer_dir.is_dir():
                files.extend(sorted(peer_dir.glob(f"*{ENVELOPE_SUFFIX}")))
        return files

    def pending_inbox(self, peer: Optional[str] = None) -> list[Path]:
        """List envelope files waiting in the inbox.

        Args:
            peer: Optional peer name to filter by.

        Returns:
            List of Path objects for pending envelope files.
        """
        if not self._inbox.exists():
            return []

        if peer:
            peer_dir = self._inbox / peer
            if not peer_dir.exists():
                return []
            return sorted(peer_dir.glob(f"*{ENVELOPE_SUFFIX}"))

        files = []
        for peer_dir in self._inbox.iterdir():
            if peer_dir.is_dir():
                files.extend(sorted(peer_dir.glob(f"*{ENVELOPE_SUFFIX}")))
        return files

    def _ensure_dirs(self) -> None:
        """Create the comms directory structure if it doesn't exist."""
        self._outbox.mkdir(parents=True, exist_ok=True)
        self._inbox.mkdir(parents=True, exist_ok=True)
        if self._archive:
            self._archive_dir.mkdir(parents=True, exist_ok=True)

    def _archive_file(self, path: Path) -> None:
        """Move a processed envelope file to the archive directory."""
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        dest = self._archive_dir / path.name
        # Reason: avoid collisions from identically-named files across peers
        if dest.exists():
            dest = self._archive_dir / f"{path.parent.name}-{path.name}"
        shutil.move(str(path), str(dest))

    @staticmethod
    def _extract_id(envelope_bytes: bytes) -> str:
        """Best-effort extraction of envelope_id from raw bytes.

        Args:
            envelope_bytes: Raw JSON envelope.

        Returns:
            The envelope_id string, or a timestamp-based fallback.
        """
        try:
            parsed = json.loads(envelope_bytes)
            return parsed.get("envelope_id", f"unknown-{int(time.time())}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"unknown-{int(time.time())}"


def create_transport(
    priority: int = 1,
    comms_root: Optional[str] = None,
    archive: bool = True,
    **kwargs,
) -> SyncthingTransport:
    """Factory function for the router's transport loader.

    Args:
        priority: Transport priority (lower = higher).
        comms_root: Override comms directory root.
        archive: Whether to archive processed envelopes.

    Returns:
        Configured SyncthingTransport instance.
    """
    root = Path(comms_root).expanduser() if comms_root else None
    return SyncthingTransport(comms_root=root, priority=priority, archive=archive)
