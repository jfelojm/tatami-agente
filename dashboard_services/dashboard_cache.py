"""Cache en memoria para respuestas pesadas de dashboards (TTL corto)."""

from __future__ import annotations

import copy
import os
import time
from typing import Any

_TTL_SEC = float(os.getenv("DASHBOARD_CACHE_TTL_SEC", "600"))
_MAX_ENTRIES = int(os.getenv("DASHBOARD_CACHE_MAX_ENTRIES", "64"))
_store: dict[str, tuple[float, Any]] = {}


def cache_ttl_sec() -> float:
    return _TTL_SEC


def get(key: str) -> Any | None:
    hit = _store.get(key)
    if not hit:
        return None
    ts, val = hit
    if (time.monotonic() - ts) >= _TTL_SEC:
        _store.pop(key, None)
        return None
    return copy.deepcopy(val)


def set(key: str, value: Any) -> None:
    if len(_store) >= _MAX_ENTRIES:
        oldest_key = min(_store.items(), key=lambda item: item[1][0])[0]
        _store.pop(oldest_key, None)
    _store[key] = (time.monotonic(), copy.deepcopy(value))


def make_key(prefix: str, **parts: object) -> str:
    items = sorted((k, str(v)) for k, v in parts.items() if v is not None and v != "")
    return prefix + "|" + "|".join(f"{k}={v}" for k, v in items)
