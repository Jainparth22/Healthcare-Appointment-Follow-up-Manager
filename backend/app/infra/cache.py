"""Cache-aside helpers with per-doctor version-counter invalidation.

Slot/profile cache keys embed a per-doctor version integer. Bumping that
integer (on any booking/cancel/leave/hours change) invalidates every key for
that doctor in O(1) — no key scans. Redis down ⇒ every op is a transparent
no-op and callers fall back to the DB.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from .redis import get_redis

logger = logging.getLogger(__name__)


def _ver_key(doctor_id: int) -> str:
    return f"cache:ver:doctor:{doctor_id}"

_SEARCH_VER_KEY = "cache:ver:doctors:search"


def doctor_version(doctor_id: int) -> int:
    """Current cache version for a doctor (1 if unknown / Redis down)."""
    client = get_redis()
    if client is None:
        return 1
    try:
        v = client.get(_ver_key(doctor_id))
        return int(v) if v is not None else 1
    except Exception:  # noqa: BLE001
        return 1


def bump_doctor_version(doctor_id: int) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        client.incr(_ver_key(doctor_id))
    except Exception:  # noqa: BLE001
        logger.debug("cache version bump failed (doctor=%s)", doctor_id)


def search_version() -> int:
    client = get_redis()
    if client is None:
        return 1
    try:
        v = client.get(_SEARCH_VER_KEY)
        return int(v) if v is not None else 1
    except Exception:  # noqa: BLE001
        return 1


def bump_search_version() -> None:
    client = get_redis()
    if client is None:
        return
    try:
        client.incr(_SEARCH_VER_KEY)
    except Exception:  # noqa: BLE001
        logger.debug("search version bump failed")


def cache_get_json(key: str) -> Optional[Any]:
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def cache_set_json(key: str, value: Any, ttl: int = 60) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        client.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception:  # noqa: BLE001
        pass


def cached_json(key: str, ttl: int, producer: Callable[[], Any]) -> Any:
    """Cache-aside: return cached value or compute, store and return it."""
    hit = cache_get_json(key)
    if hit is not None:
        return hit
    value = producer()
    cache_set_json(key, value, ttl)
    return value
