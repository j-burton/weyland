"""Bearer token validation. The token's sha256 is what's checked."""
from __future__ import annotations

import hashlib
import hmac


def token_matches(presented: str, expected_hash_hex: str) -> bool:
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest.lower(), expected_hash_hex.lower())
