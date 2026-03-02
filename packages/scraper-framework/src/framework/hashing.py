"""Content hashing utilities — SHA-256 per Architecture Spec §3.1.1."""

from __future__ import annotations

import hashlib


def sha256_hex(content: bytes) -> str:
    """Return the SHA-256 hex digest of content."""
    return hashlib.sha256(content).hexdigest()


def content_changed(old_hash: str, new_content: bytes) -> bool:
    """Return True if new_content has a different hash from old_hash."""
    return sha256_hex(new_content) != old_hash
