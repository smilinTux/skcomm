"""
File transport — the simplest possible delivery mechanism.

Writes envelopes as JSON files to a shared directory. Works over
NFS, SSHFS, CIFS, USB drives, Nextcloud sync, or any shared
filesystem. No network stack, no daemons, no configuration beyond
a pair of paths.

This is the sneakernet transport. If you can copy a file, you can
deliver a message.

Directory layout:
    outbox_path/          # Sender writes here
    └── {id}.skc.json     # One file per envelope

    inbox_path/           # Receiver reads here
    └── {id}.skc.json     # Appears when the filesystem syncs
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from ..transport import (
    HealthStatus,
    SendResult,
    Transport,
    TransportCategory,
    TransportStatus,
)

logger = logging.getLogger("skcomm.transports.file")

ENVELOPE_SUFFIX = ".skc.json"


class FileTransport(Transport):
    """Filesystem-based transport for shared directories.

    Unlike the Syncthing transport which uses per-peer subdirectories,
    the file transport uses flat outbox/inbox paths. The filesystem
    sharing mechanism (NFS, SSHFS, Nextcloud, USB) handles propagation.

    Attributes:
        name: Always "file".
        priority: Default 2 (after Syncthing, which is always-on).
        category: FILE_BASED — works offline, pure filesystem I/O.
    """

    name: str = "file"
    priority: int = 2
    category: TransportCategory = TransportCategory.FILE_BASED

    def __init__(
        self,
        outbox_path: Optional[Path] = None,
        inbox_path: Optional[Path] = None,
        priority: int = 2,
        archive: bool = True,
        archive_path: Optional[Path] = None,
        poll_interval_ms: int = 1000,
        **kwargs,
    ):
        """Initialize the file transport.

        Args:
            outbox_path: Directory to write outgoing envelopes.
            inbox_path: Directory to read incoming envelopes.
            priority: Transport priority (lower = higher).
            archive: Whether to archive processed inbox files.
            archive_path: Override archive directory location.
            poll_interval_ms: Suggested polling interval (informational).
        """
        self.priority = priority
        self._archive = archive
        self._poll_interval_ms = poll_interval_ms

        self._outbox = Path(outbox_path).expanduser() if outbox_path else Path("~/.skcomm/outbox").expanduser()
        self._inbox = Path(inbox_path).expanduser() if inbox_path else Path("~/.skcomm/inbox").expanduser()
        self._archive_dir = (
            Path(archive_path).expanduser()
            if archive_path
            else self._inbox.parent / "archive"
        )

    def configure(self, config: dict) -> None:
        """Load transport-specific configuration.

        Args:
            config: Dict with optional keys: outbox_path, inbox_path,
                    archive, archive_path, poll_interval_ms.
        """
        if "outbox_path" in config:
            self._outbox = Path(config["outbox_path"]).expanduser()
        if "inbox_path" in config:
            self._inbox = Path(config["inbox_path"]).expanduser()
        if "archive_path" in config:
            self._archive_dir = Path(config["archive_path"]).expanduser()
        if "archive" in config:
            self._archive = config["archive"]
        if "poll_interval_ms" in config:
            self._poll_interval_ms = config["poll_interval_ms"]

    def is_available(self) -> bool:
        """Check if outbox and inbox directories are accessible.

        Returns:
            True if directories exist or can be created.
        """
        try:
            self._outbox.mkdir(parents=True, exist_ok=True)
            self._inbox.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

    def send(self, envelope_bytes: bytes, recipient: str) -> SendResult:
        """Write an envelope file to the outbox directory.

        Atomic write (tmp then rename) to prevent readers from
        seeing partial files.

        Args:
            envelope_bytes: Serialized MessageEnvelope bytes.
            recipient: Recipient identifier (logged, not used for routing).

        Returns:
            SendResult with success/failure and timing.
        """
        start = time.monotonic()
        envelope_id = self._extract_id(envelope_bytes)

        try:
            self._outbox.mkdir(parents=True, exist_ok=True)

            filename = f"{envelope_id}{ENVELOPE_SUFFIX}"
            target = self._outbox / filename
            tmp_target = self._outbox / f".{filename}.tmp"

            tmp_target.write_bytes(envelope_bytes)
            tmp_target.rename(target)

            elapsed = (time.monotonic() - start) * 1000
            logger.info("Wrote %s to %s (%0.1fms)", envelope_id[:8], target, elapsed)

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
        """Poll the inbox directory for new envelope files.

        Returns:
            List of raw envelope bytes from inbox files.
        """
        received: list[bytes] = []

        if not self._inbox.exists():
            return received

        for env_file in sorted(self._inbox.glob(f"*{ENVELOPE_SUFFIX}")):
            if env_file.name.startswith("."):
                continue

            try:
                data = env_file.read_bytes()
                received.append(data)

                if self._archive:
                    self._archive_file(env_file)
                else:
                    env_file.unlink()

                logger.debug("Received: %s", env_file.name)

            except OSError as exc:
                logger.warning("Failed to read %s: %s", env_file, exc)

        return received

    def health_check(self) -> HealthStatus:
        """Check filesystem accessibility and report status.

        Returns:
            HealthStatus with directory info and pending counts.
        """
        start = time.monotonic()
        details: dict = {}

        try:
            self._outbox.mkdir(parents=True, exist_ok=True)
            self._inbox.mkdir(parents=True, exist_ok=True)
            latency = (time.monotonic() - start) * 1000

            outbox_count = len(list(self._outbox.glob(f"*{ENVELOPE_SUFFIX}")))
            inbox_count = len(list(self._inbox.glob(f"*{ENVELOPE_SUFFIX}")))

            details = {
                "outbox_path": str(self._outbox),
                "inbox_path": str(self._inbox),
                "pending_outbox": outbox_count,
                "pending_inbox": inbox_count,
                "poll_interval_ms": self._poll_interval_ms,
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

    def _archive_file(self, path: Path) -> None:
        """Move a processed file to the archive directory."""
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        dest = self._archive_dir / path.name
        if dest.exists():
            dest = self._archive_dir / f"{int(time.time())}-{path.name}"
        shutil.move(str(path), str(dest))

    @staticmethod
    def _extract_id(envelope_bytes: bytes) -> str:
        """Best-effort envelope_id extraction from raw bytes."""
        try:
            parsed = json.loads(envelope_bytes)
            return parsed.get("envelope_id", f"unknown-{int(time.time())}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"unknown-{int(time.time())}"


def create_transport(
    priority: int = 2,
    outbox_path: Optional[str] = None,
    inbox_path: Optional[str] = None,
    archive: bool = True,
    **kwargs,
) -> FileTransport:
    """Factory function for the router's transport loader.

    Args:
        priority: Transport priority (lower = higher).
        outbox_path: Override outbox directory.
        inbox_path: Override inbox directory.
        archive: Whether to archive processed files.

    Returns:
        Configured FileTransport instance.
    """
    return FileTransport(
        outbox_path=Path(outbox_path) if outbox_path else None,
        inbox_path=Path(inbox_path) if inbox_path else None,
        priority=priority,
        archive=archive,
    )
