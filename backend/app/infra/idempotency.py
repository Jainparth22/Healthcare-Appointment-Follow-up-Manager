"""Idempotency-Key store/replay.

First response for a given key is cached; subsequent requests with the same
key replay it verbatim — killing double-clicks and client retries. Redis down
⇒ no replay, which is safe because the booking CAS already prevents duplicate
state changes; the key just becomes a no-op.
"""
from __future__ import annotations

import json
from typing import Optional

from .redis import get_redis

_TTL_SECONDS = 24 * 3600


def _key(scope: str, idem_key: str, user_id: int) -> str:
    return f"idem:{scope}:{user_id}:{idem_key}"


def get_cached(scope: str, idem_key: str, user_id: int) -> Optional[dict]:
    client = get_redis()
    if client is None or not idem_key:
        return None
    try:
        raw = client.get(_key(scope, idem_key, user_id))
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def store(scope: str, idem_key: str, user_id: int, status_code: int, body: dict) -> None:
    client = get_redis()
    if client is None or not idem_key:
        return
    try:
        payload = json.dumps({"status_code": status_code, "body": body}, default=str)
        # NX so a concurrent duplicate can't overwrite the first winner.
        client.set(_key(scope, idem_key, user_id), payload, ex=_TTL_SECONDS, nx=True)
    except Exception:  # noqa: BLE001
        pass
