"""
SKComm — Transport-agnostic encrypted communication for sovereign AI.

One message. Many paths. Always delivered.

The postal service model: separate the message from the medium.
The envelope format never changes. Only the delivery mechanism varies.
"""

__version__ = "0.1.1"

from .core import SKComm
from .models import (
    MessageEnvelope,
    MessageMetadata,
    MessagePayload,
    MessageType,
    RoutingConfig,
    RoutingMode,
)
from .transport import HealthStatus, SendResult, Transport, TransportError, TransportStatus
from .crypto import EnvelopeCrypto, KeyStore
from .signing import EnvelopeSigner, EnvelopeVerifier, SignedEnvelope, VerificationResult

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
