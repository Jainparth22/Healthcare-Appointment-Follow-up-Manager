"""Redis distributed lock (SET NX PX + Lua compare-and-delete).

The lock is an *optimisation* — a fast cross-node rejection that avoids doing
the DB CAS at all when another node clearly holds the slot. It is NOT the
source of truth: if Redis is unavailable the lock is a no-op and correctness
still holds via the compare-and-swap + unique index in the DB.
"""
from __future__ import annotations

import contextlib
import logging
import secrets

from ..config import settings
from .redis import get_redis

logger = logging.getLogger(__name__)

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@contextlib.contextmanager
def slot_lock(slot_id: int, ttl_ms: int | None = None):
    """Best-effort distributed lock around a slot.

    Yields ``True`` if the lock was acquired (or Redis is down — fail-open so
    the DB CAS remains the guarantee), ``False`` if another holder owns it.
    """
    client = get_redis()
    if client is None:
        yield True  # fail-open: DB CAS is the real guard
        return

    key = f"lock:slot:{slot_id}"
    token = secrets.token_hex(16)
    ttl = ttl_ms or settings.LOCK_TTL_MS
    acquired = False
    try:
        acquired = bool(client.set(key, token, nx=True, px=ttl))
        yield acquired
    except Exception as exc:  # noqa: BLE001
        logger.warning("slot lock error (%s); failing open", exc)
        yield True
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                client.eval(_RELEASE_LUA, 1, key, token)
