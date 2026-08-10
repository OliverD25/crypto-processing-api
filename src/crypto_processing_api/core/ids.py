"""Time-ordered identifiers.

UUIDv7 sorts by creation time, so primary keys stay index-friendly and an
operator reading a list of deposit ids can see their order. `uuid.uuid7` only
exists from Python 3.14; this is the RFC 9562 layout built by hand so the
project still runs on 3.12.
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    timestamp_ms = time.time_ns() // 1_000_000
    raw = bytearray(os.urandom(16))
    raw[0:6] = timestamp_ms.to_bytes(6, "big")
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))
