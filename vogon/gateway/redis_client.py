"""Async Redis cluster client singleton (the gateway's own; vogon step 2, slice 5).

ElastiCache Serverless (and MemoryDB) are always cluster-mode, so we use
RedisCluster from the start.  For local dev with redis_enabled=False the
client returns None — callers treat None as "fall back to in-process store."

Connection is lazy: the first call to get_redis() establishes the cluster
topology handshake.  Subsequent calls return the cached client.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from vogon.gateway import settings as _gw_settings

if TYPE_CHECKING:
    from redis.asyncio.cluster import RedisCluster

logger = logging.getLogger(__name__)

_redis: RedisCluster | None = None


async def get_redis() -> RedisCluster | None:
    """Return the shared RedisCluster client, or None if Redis is disabled."""

    if not _gw_settings.settings.redis_enabled:
        return None

    global _redis
    if _redis is None:
        _kwargs = dict(
            decode_responses=True,
            # socket_timeout must exceed the longest blocking command in the
            # codebase (currently xreadgroup block=30 000 ms in job_processor).
            # Without this, the socket read times out before xreadgroup returns,
            # raising redis.exceptions.TimeoutError on every idle worker loop.
            socket_timeout=40,
            socket_connect_timeout=5,
            retry=Retry(ExponentialBackoff(cap=0.5, base=0.1), 3),
        )
        if _gw_settings.settings.redis_cluster_mode:
            _redis = RedisCluster.from_url(_gw_settings.settings.redis_url, **_kwargs)
        else:
            # Standalone Redis (docker-compose local dev).
            _redis = aioredis.from_url(_gw_settings.settings.redis_url, **_kwargs)
        logger.info(
            "Redis client initialised (%s): %s",
            "cluster" if _gw_settings.settings.redis_cluster_mode else "standalone",
            _gw_settings.settings.redis_url,
        )

    return _redis


async def close_redis() -> None:
    """Close the cluster connection pool.  Called at application shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
