"""
SKComm — Transport-agnostic encrypted communication for sovereign AI.

One message. Many paths. Always delivered.

The postal service model: separate the message from the medium.
The envelope format never changes. Only the delivery mechanism varies.
"""

__version__ = "0.1.0"

from .core import SKComm
from .models import (
    MessageEnvelope,
    MessageMetadata,
    MessagePayload,
    MessageType,
    RoutingConfig,
    RoutingMode,
)
from .transport import HealthStatus, SendResult, Transport, TransportStatus
from .crypto import EnvelopeCrypto, KeyStore

__all__ = [
    "SKComm",
    "MessageEnvelope",
    "MessageMetadata",
    "MessagePayload",
    "MessageType",
    "RoutingConfig",
    "RoutingMode",
    "Transport",
    "TransportStatus",
    "HealthStatus",
    "SendResult",
    "EnvelopeCrypto",
    "KeyStore",
]
