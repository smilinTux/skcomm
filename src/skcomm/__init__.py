"""
SKComm — Transport-agnostic encrypted communication for sovereign AI.

One message. Many paths. Always delivered.

The postal service model: separate the message from the medium.
The envelope format never changes. Only the delivery mechanism varies.
"""

__version__ = "0.1.1"

from .core import SKComm
from .crypto import EnvelopeCrypto, KeyStore
from .models import (
    MessageEnvelope,
    MessageMetadata,
    MessagePayload,
    MessageType,
    RoutingConfig,
    RoutingMode,
)
from .signing import EnvelopeSigner, EnvelopeVerifier, SignedEnvelope, VerificationResult
from .transport import HealthStatus, SendResult, Transport, TransportError, TransportStatus

__all__ = [
    "SKComm",
    "MessageEnvelope",
    "MessageMetadata",
    "MessagePayload",
    "MessageType",
    "RoutingConfig",
    "RoutingMode",
    "Transport",
    "TransportError",
    "TransportStatus",
    "HealthStatus",
    "SendResult",
    "EnvelopeCrypto",
    "KeyStore",
    "SignedEnvelope",
    "EnvelopeSigner",
    "EnvelopeVerifier",
    "VerificationResult",
]
