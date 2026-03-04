"""Pytest integration test for the skcomm Nostr transport.

Uses pytest-asyncio and a mock WebSocket Nostr relay to verify:
- configure() sets the relay URL used by subsequent operations
- send() publishes a properly signed NIP-59 gift-wrap event
- receive() returns decoded envelope bytes within a 5 s timeout
- health_check() reports AVAILABLE when the relay responds

Relay I/O is intercepted at the _ws_connect layer so no network
connection is required. The mock relay speaks the Nostr REQ / EVENT /
EOSE / OK wire protocol.

Coord task: 39ff64de
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Callable
from unittest.mock import patch

import pytest

from skcomm.transport import TransportStatus
from skcomm.transports.nostr import (
    KIND_GIFT_WRAP,
    NOSTR_AVAILABLE,
    NostrTransport,
    _pubkey_of,
    _random_secret,
    wrap_dm,
)

pytestmark = pytest.mark.skipif(
    not NOSTR_AVAILABLE,
    reason="Nostr deps not installed (pip install skcomm[nostr])",
)


# ---------------------------------------------------------------------------
# Mock relay infrastructure
# ---------------------------------------------------------------------------


class MockWSConnection:
    """Simulates a Nostr relay WebSocket connection.

    Records outbound messages, optionally invokes a send-callback to
    generate relay responses, and serves them on the next recv() call.
    """

    def __init__(self) -> None:
        self._responses: list[str] = []
        self._send_callback: Callable[[list], str | None] | None = None
        self.sent: list[list] = []

    def send(self, data: str) -> None:
        msg: list = json.loads(data)
        self.sent.append(msg)
        if self._send_callback:
            reply = self._send_callback(msg)
            if reply is not None:
                self._responses.append(reply)

    def recv(self, timeout: float | None = None) -> str:  # noqa: ARG002
        if not self._responses:
            raise TimeoutError("MockWSConnection: response queue exhausted")
        return self._responses.pop(0)

    def close(self) -> None:
        pass

    def __enter__(self) -> "MockWSConnection":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


class MockNostrRelay:
    """Callable that returns MockWSConnections simulating a Nostr relay.

    Pass as ``side_effect`` of ``patch("skcomm.transports.nostr._ws_connect")``.

    The relay:
    - Records every URL the transport connects to.
    - Responds to EVENT frames with an OK acceptance.
    - Responds to REQ frames with pre-staged events then EOSE.
    - Allows plain open/close for health-check probes (no messages needed).
    """

    def __init__(self) -> None:
        self.connected_urls: list[str] = []
        self.published_events: list[dict] = []
        self._staged: list[dict] = []

    def stage_event(self, event: dict) -> None:
        """Pre-load an event to return on the next REQ query."""
        self._staged.append(event)

    def __call__(self, url: str, **_kwargs: Any) -> MockWSConnection:
        self.connected_urls.append(url)
        ws = MockWSConnection()
        relay = self

        def handle_send(msg: list) -> str | None:
            verb = msg[0] if msg else None
            if verb == "EVENT":
                event: dict = msg[1]
                relay.published_events.append(event)
                return json.dumps(["OK", event.get("id", ""), True, ""])
            if verb == "REQ":
                sub_id: str = msg[1]
                for ev in relay._staged:
                    ws._responses.append(json.dumps(["EVENT", sub_id, ev]))
                return json.dumps(["EOSE", sub_id])
            # CLOSE and other verbs need no relay response
            return None

        ws._send_callback = handle_send
        return ws


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay() -> MockNostrRelay:
    """Fresh mock relay per test."""
    return MockNostrRelay()


@pytest.fixture()
def sender() -> NostrTransport:
    """Nostr transport used as the sending agent."""
    return NostrTransport(relays=["wss://mock.relay.local"], relay_timeout=5.0)


@pytest.fixture()
def receiver() -> NostrTransport:
    """Nostr transport used as the receiving agent."""
    return NostrTransport(relays=["wss://mock.relay.local"], relay_timeout=5.0)


@pytest.fixture()
def sample_envelope_bytes() -> bytes:
    """Minimal serialised SKComm envelope."""
    return json.dumps({
        "skcomm_version": "1.0.0",
        "envelope_id": "nostr-test-39ff64de",
        "sender": "jarvis",
        "recipient": "lumina",
        "payload": {"content": "staycuriousANDkeepsmilin", "content_type": "text"},
    }).encode()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_configure_connects_to_relay(relay: MockNostrRelay) -> None:
    """configure() sets the relay URL; health_check() connects to it.

    After configure() updates the relay list, health_check() must open a
    WebSocket to the newly configured URL and report AVAILABLE.
    """
    t = NostrTransport(relays=["wss://old.relay.local"])
    configured_url = "wss://configured.relay.local"
    t.configure({"relays": [configured_url]})

    with patch("skcomm.transports.nostr._ws_connect", side_effect=relay):
        health = t.health_check()

    assert configured_url in relay.connected_urls, (
        "health_check() must connect to the URL set by configure()"
    )
    assert health.status == TransportStatus.AVAILABLE


async def test_send_publishes_signed_event(
    sender: NostrTransport,
    relay: MockNostrRelay,
    sample_envelope_bytes: bytes,
) -> None:
    """send() publishes a kind-1059 gift-wrap with a valid BIP-340 signature.

    The transport must:
    - Wrap the envelope in a NIP-59 gift wrap (kind 1059).
    - Sign it with a 64-byte BIP-340 Schnorr signature (128 hex chars).
    - Return a successful SendResult when the relay replies OK.
    """
    recipient_secret = _random_secret()
    rx, _ = _pubkey_of(recipient_secret)

    with patch("skcomm.transports.nostr._ws_connect", side_effect=relay):
        result = sender.send(sample_envelope_bytes, rx.hex())

    assert result.success is True, f"send() failed: {result.error}"
    assert len(relay.published_events) == 1, "Exactly one event must reach the relay"

    event = relay.published_events[0]
    assert event["kind"] == KIND_GIFT_WRAP, (
        f"Expected kind {KIND_GIFT_WRAP} (gift wrap), got {event['kind']}"
    )
    assert len(event.get("sig", "")) == 128, (
        "BIP-340 Schnorr signature must be 128 hex characters"
    )
    assert "id" in event, "Published event must contain an id field"


async def test_receive_returns_event_within_5s_timeout(
    sender: NostrTransport,
    receiver: NostrTransport,
    relay: MockNostrRelay,
    sample_envelope_bytes: bytes,
) -> None:
    """receive() returns the decoded envelope within a 5 s timeout.

    A gift-wrap produced by the sender is pre-loaded into the mock relay.
    receive() must unwrap it and return the original bytes, completing
    before the 5 second asyncio deadline.
    """
    # Build the gift-wrap as send() would
    gift = wrap_dm(
        sender._secret,
        sender.pubkey,
        receiver.pubkey,
        base64.b64encode(sample_envelope_bytes).decode(),
    )
    relay.stage_event(gift)

    loop = asyncio.get_running_loop()
    with patch("skcomm.transports.nostr._ws_connect", side_effect=relay):
        received = await asyncio.wait_for(
            loop.run_in_executor(None, receiver.receive),
            timeout=5.0,
        )

    assert len(received) == 1, "receive() must return exactly one envelope"
    payload = json.loads(received[0])
    assert payload["envelope_id"] == "nostr-test-39ff64de", (
        "Unwrapped envelope must match the original envelope_id"
    )


async def test_health_check_returns_available(relay: MockNostrRelay) -> None:
    """health_check() reports AVAILABLE when the relay accepts the connection.

    The mock relay allows plain open/close; health_check() must count the
    relay as reachable and set status to AVAILABLE.
    """
    t = NostrTransport(relays=["wss://mock.relay.local"])

    with patch("skcomm.transports.nostr._ws_connect", side_effect=relay):
        health = t.health_check()

    assert health.status == TransportStatus.AVAILABLE
    assert health.details["reachable_relays"] == 1
    assert health.details["total_relays"] == 1
