"""Lazy Redis client that never crashes the app when Redis is unavailable."""
from __future__ import annotations

import logging
import time

from ..config import settings

logger = logging.getLogger(__name__)

try:  # redis is a hard dependency, but import defensively anyway
    import redis as _redis
except Exception:  # pragma: no cover
    _redis = None

_client = None
_last_check = 0.0
_RECHECK_SECONDS = 15.0  # allow reconnection if Redis comes back


def get_redis():
    """Return a live Redis client, or ``None`` if unavailable.

    The result is cached and re-probed at most every ``_RECHECK_SECONDS`` so a
    Redis that comes back online is picked up without a restart.
    """
    global _client, _last_check
    now = time.monotonic()
    if _client is not None:
        return _client
    if _redis is None:
        return None
    if now - _last_check < _RECHECK_SECONDS:
        return None
    _last_check = now
    try:
        client = _redis.Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )
        client.ping()
        _client = client
        logger.info("Redis connected")
        return _client
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis unavailable (%s); degrading gracefully", exc)
        _client = None
        return None


def redis_healthy() -> bool:
    client = get_redis()
    if client is None:
        return False
    try:
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def reset_client() -> None:
    """Force re-probe on next call (used by tests)."""
    global _client, _last_check
    _client = None
    _last_check = 0.0
