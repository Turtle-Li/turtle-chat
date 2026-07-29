from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field


TIMESTAMP_HEADER = "X-Turtle-Pump-Timestamp"
NONCE_HEADER = "X-Turtle-Pump-Nonce"
SIGNATURE_HEADER = "X-Turtle-Pump-Signature"


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_request(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join(
        [method.upper(), path, timestamp, nonce, body_digest(body)]
    ).encode("utf-8")


def sign_request(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical_request(method, path, timestamp, nonce, body),
        hashlib.sha256,
    ).hexdigest()


@dataclass(slots=True)
class NonceGuard:
    ttl_seconds: int
    _seen: dict[str, float] = field(default_factory=dict)

    def accept(self, nonce: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        cutoff = current - self.ttl_seconds
        self._seen = {key: value for key, value in self._seen.items() if value >= cutoff}
        if nonce in self._seen:
            return False
        self._seen[nonce] = current
        return True
