"""Small shared helpers (leaf module: no app imports, so dissectors can use it
without risking an import cycle)."""

from __future__ import annotations

import json


def safe_json(data) -> dict | None:
    """A body parsed as a JSON object, else None (empty/invalid/non-object)."""
    if not data:
        return None
    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        obj = json.loads(data)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
