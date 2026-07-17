"""Small canonical JSON primitive shared without contract/redaction coupling."""

from __future__ import annotations

import json
from typing import Any


def canonical_json_value_bytes(value: Any) -> bytes:
    """Serialize a JSON-like value in the TraceHelix canonical byte form."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
