"""Redis token-bucket rate limiter.

Fails **open** when Redis is unavailable (availability > strictness for a demo
clinic app). Used to blunt brute-force on login/register and abuse of booking.
"""
from __future__ import annotations

from ..config import settings
from .redis import get_redis

# Refill-on-read token bucket implemented in Lua for atomicity.
_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local data = redis.call('hmget', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
    tokens = capacity
    ts = now
end
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill)
local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end
redis.call('hmset', key, 'tokens', tokens, 'ts', now)
redis.call('expire', key, math.ceil(capacity / refill) + 1)
return allowed
"""


def allow(name: str, identity: str, limit: int, window_seconds: int) -> bool:
    """Return True if the action is allowed under ``limit`` per ``window``."""
    if not settings.RATE_LIMIT_ENABLED:
        return True
    client = get_redis()
    if client is None:
        return True  # fail open
    key = f"rl:{name}:{identity}"
    refill = limit / float(window_seconds)
    try:
        import time

        allowed = client.eval(_BUCKET_LUA, 1, key, limit, refill, time.time(), 1)
        return bool(int(allowed))
    except Exception:  # noqa: BLE001
        return True
