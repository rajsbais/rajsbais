"""RFC adapter settings (read late so tests and the CLI can override them through the environment)."""
from __future__ import annotations

import os


def transport_mode() -> str:
    return os.getenv("SDTF_RFC_TRANSPORT", "auto")  # auto | pyrfc | simulated


def package_size() -> int:
    return int(os.getenv("SDTF_RFC_PACKAGE_SIZE", "5000"))


def key_chunk() -> int:
    """How many EQ ranges on one field are pushed down per call (ABAP range tables of this size are cheap)."""
    return int(os.getenv("SDTF_RFC_KEY_CHUNK", "200"))


def key_pushdown_limit() -> int:
    """Partitions with at most this many objects push their keys down; larger ones push the organisational
    predicate only and filter keys client-side (fewer calls, more rows transferred)."""
    return int(os.getenv("SDTF_RFC_KEY_PUSHDOWN_LIMIT", "2000"))
