"""
SKComm heartbeat protocol — alive/dead detection across the mesh.

Each agent periodically writes a lightweight heartbeat file to the
shared comms directory. Syncthing propagates it to all peers. Any
agent can read the heartbeat files to determine who is alive.

Heartbeat file layout:
    {comms_root}/heartbeats/{agent_name}.json

Liveness classification:
    ALIVE:   heartbeat received within alive_timeout (default 2 min)
    STALE:   heartbeat received within stale_timeout (default 5 min)
    DEAD:    no heartbeat for longer than stale_timeout
    UNKNOWN: never seen a heartbeat from this peer
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcomm.heartbeat")

HEARTBEAT_DIR = "heartbeats"
HEARTBEAT_SUFFIX = ".heartbeat.json"

DEFAULT_ALIVE_TIMEOUT = 120
DEFAULT_STALE_TIMEOUT = 300
DEFAULT_COMMS_ROOT = "~/.skcapstone/comms"


class PeerLiveness(str, Enum):
    """Liveness state of a peer based on heartbeat timing."""

    ALIVE = "alive"
    STALE = "stale"
    DEAD = "dead"
    UNKNOWN = "unknown"


class HeartbeatPayload(BaseModel):
    """Data written to the heartbeat file.

    Attributes:
        agent: Agent name (matches the filename).
        timestamp: When this heartbeat was emitted (UTC ISO format).
        fingerprint: PGP fingerprint, if available.
        nostr_pubkey: Nostr x-only hex pubkey, if available.
        transports: List of transport names this agent supports.
        version: Heartbeat protocol version.
    """

    agent: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fingerprint: Optional[str] = None
    nostr_pubkey: Optional[str] = None
    transports: list[str] = Field(default_factory=list)
    version: str = "1.0.0"


class PeerHeartbeat(BaseModel):
    """Status report for a single peer.

    Attributes:
        name: Agent name.
        status: Current liveness state.
        last_heartbeat: When the peer last emitted a heartbeat.
        age_seconds: Seconds since the last heartbeat.
        transports: What transports the peer reported supporting.
        fingerprint: PGP fingerprint from the heartbeat.
    """

    name: str
    status: PeerLiveness
    last_heartbeat: Optional[datetime] = None
    age_seconds: Optional[float] = None
    transports: list[str] = Field(default_factory=list)
    fingerprint: Optional[str] = None


class HeartbeatMonitor:
    """File-based heartbeat monitor for mesh peer liveness.

    Writes heartbeat files to the shared comms directory and
    reads peer heartbeats to determine their liveness status.
    Designed to work with Syncthing propagation.

    Args:
        agent_name: This agent's name.
        comms_root: Root of the shared comms directory.
        fingerprint: PGP fingerprint to include in heartbeats.
        nostr_pubkey: Nostr pubkey to include in heartbeats.
        transports: List of transport names this agent supports.
        alive_timeout: Seconds before a peer is considered stale.
        stale_timeout: Seconds before a peer is considered dead.
    """

    def __init__(
        self,
        agent_name: str,
        comms_root: Optional[Path] = None,
        fingerprint: Optional[str] = None,
        nostr_pubkey: Optional[str] = None,
        transports: Optional[list[str]] = None,
        alive_timeout: int = DEFAULT_ALIVE_TIMEOUT,
        stale_timeout: int = DEFAULT_STALE_TIMEOUT,
    ):
        self._name = agent_name
        self._root = (comms_root or Path(DEFAULT_COMMS_ROOT)).expanduser()
        self._hb_dir = self._root / HEARTBEAT_DIR
        self._fingerprint = fingerprint
        self._nostr_pubkey = nostr_pubkey
        self._transports = transports or []
        self._alive_timeout = alive_timeout
        self._stale_timeout = stale_timeout

    @property
    def heartbeat_dir(self) -> Path:
        """Directory where heartbeat files are stored."""
        return self._hb_dir

    def emit(self) -> Path:
        """Write this agent's heartbeat file.

        Creates or overwrites the heartbeat JSON file in the
        shared comms directory. Syncthing propagates the updated
        file to all connected peers.

        Returns:
            Path to the written heartbeat file.
        """
        self._hb_dir.mkdir(parents=True, exist_ok=True)
        payload = HeartbeatPayload(
            agent=self._name,
            fingerprint=self._fingerprint,
            nostr_pubkey=self._nostr_pubkey,
            transports=self._transports,
        )
        path = self._hb_dir / f"{self._name}{HEARTBEAT_SUFFIX}"
        tmp = self._hb_dir / f".{self._name}{HEARTBEAT_SUFFIX}.tmp"

        data = payload.model_dump_json(indent=2)
        tmp.write_text(data)
        tmp.rename(path)

        logger.debug("Emitted heartbeat to %s", path)
        return path

    def read_peer(self, peer_name: str) -> Optional[HeartbeatPayload]:
        """Read a specific peer's heartbeat file.

        Args:
            peer_name: Name of the peer to check.

        Returns:
            HeartbeatPayload or None if no heartbeat file exists.
        """
        path = self._hb_dir / f"{peer_name}{HEARTBEAT_SUFFIX}"
        if not path.exists():
            return None
        try:
            return HeartbeatPayload.model_validate_json(path.read_text())
        except Exception as exc:
            logger.warning("Failed to read heartbeat for %s: %s", peer_name, exc)
            return None

    def peer_status(self, peer_name: str) -> PeerHeartbeat:
        """Check the liveness status of a single peer.

        Args:
            peer_name: Name of the peer.

        Returns:
            PeerHeartbeat with current status and timing.
        """
        payload = self.read_peer(peer_name)
        if payload is None:
            return PeerHeartbeat(name=peer_name, status=PeerLiveness.UNKNOWN)

        now = datetime.now(timezone.utc)
        age = (now - payload.timestamp).total_seconds()
        status = self._classify(age)

        return PeerHeartbeat(
            name=peer_name,
            status=status,
            last_heartbeat=payload.timestamp,
            age_seconds=round(age, 1),
            transports=payload.transports,
            fingerprint=payload.fingerprint,
        )

    def scan(self) -> list[PeerHeartbeat]:
        """Scan the heartbeat directory for all peer statuses.

        Returns:
            List of PeerHeartbeat for every peer with a heartbeat file,
            sorted by name. Excludes this agent's own heartbeat.
        """
        if not self._hb_dir.exists():
            return []

        results: list[PeerHeartbeat] = []
        now = datetime.now(timezone.utc)

        for path in sorted(self._hb_dir.glob(f"*{HEARTBEAT_SUFFIX}")):
            if path.name.startswith("."):
                continue
            peer_name = path.name.replace(HEARTBEAT_SUFFIX, "")
            if peer_name == self._name:
                continue

            try:
                payload = HeartbeatPayload.model_validate_json(path.read_text())
                age = (now - payload.timestamp).total_seconds()
                results.append(PeerHeartbeat(
                    name=peer_name,
                    status=self._classify(age),
                    last_heartbeat=payload.timestamp,
                    age_seconds=round(age, 1),
                    transports=payload.transports,
                    fingerprint=payload.fingerprint,
                ))
            except Exception as exc:
                logger.warning("Invalid heartbeat file %s: %s", path.name, exc)
                results.append(PeerHeartbeat(
                    name=peer_name, status=PeerLiveness.UNKNOWN,
                ))

        return results

    def all_statuses(self, include_self: bool = False) -> list[PeerHeartbeat]:
        """Get statuses for all peers, optionally including self.

        Args:
            include_self: Whether to include this agent's own heartbeat.

        Returns:
            List of PeerHeartbeat sorted by name.
        """
        results = self.scan()
        if include_self:
            self_status = self.peer_status(self._name)
            if self_status.status != PeerLiveness.UNKNOWN:
                results.insert(0, self_status)
        return results

    def alive_peers(self) -> list[str]:
        """Return names of peers currently considered alive.

        Returns:
            List of peer names with ALIVE status.
        """
        return [p.name for p in self.scan() if p.status == PeerLiveness.ALIVE]

    def dead_peers(self) -> list[str]:
        """Return names of peers currently considered dead.

        Returns:
            List of peer names with DEAD status.
        """
        return [p.name for p in self.scan() if p.status == PeerLiveness.DEAD]

    def _classify(self, age_seconds: float) -> PeerLiveness:
        """Classify a peer's liveness based on heartbeat age.

        Args:
            age_seconds: Seconds since last heartbeat.

        Returns:
            PeerLiveness classification.
        """
        if age_seconds <= self._alive_timeout:
            return PeerLiveness.ALIVE
        elif age_seconds <= self._stale_timeout:
            return PeerLiveness.STALE
        else:
            return PeerLiveness.DEAD
