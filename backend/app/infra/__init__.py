"""Infra helpers: Redis client, cache, locks, idempotency, rate limiting.

Every helper degrades gracefully when Redis is down or absent — the app stays
fully functional (correctness is guaranteed by the DB, not by Redis).
"""
