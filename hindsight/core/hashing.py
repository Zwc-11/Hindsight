"""Stable content hashes for deterministic runs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256


def stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def run_hash(records: Sequence[Mapping[str, object]]) -> str:
    return stable_hash(list(records))
