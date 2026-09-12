import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    """Redis-based caching service for API responses and frequently accessed data."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.default_ttl = 300  # 5 minutes

    async def initialize(self):
        """
        Connect to Redis. A failure is non-fatal: callers treat `redis is None`
        as "no cache", and the rate limiter falls back to in-process buckets.
        """
        client = None
        try:
            client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
            await client.ping()
            self.redis = client
            logger.info("Redis cache connected successfully")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            # Release the pool the failed client allocated. Assigning None over
            # it would leak the connection pool on every retry.
            if client is not None:
                try:
                    closer = getattr(client, "aclose", None) or client.close
                    await closer()
                except Exception:
                    pass
            self.redis = None

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        if not self.redis:
            return None
        try:
            value = await self.redis.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.warning(f"Cache get error for key={key}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Set value in cache with TTL."""
        if not self.redis:
            return False
        try:
            serialized = json.dumps(value, default=str)
            await self.redis.set(key, serialized, ex=ttl or self.default_ttl)
            return True
        except Exception as e:
            logger.warning(f"Cache set error for key={key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete value from cache."""
        if not self.redis:
            return False
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            logger.warning(f"Cache delete error for key={key}: {e}")
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching pattern."""
        if not self.redis:
            return 0
        try:
            keys = []
            async for key in self.redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                await self.redis.delete(*keys)
            return len(keys)
        except Exception as e:
            logger.warning(f"Cache delete_pattern error for pattern={pattern}: {e}")
            return 0

    async def increment(self, key: str, amount: int = 1) -> Optional[int]:
        """Increment a counter in cache."""
        if not self.redis:
            return None
        try:
            return await self.redis.incr(key, amount)
        except Exception as e:
            logger.warning(f"Cache increment error for key={key}: {e}")
            return None

    async def cleanup(self):
        """Close the Redis connection and drop the reference."""
        if self.redis:
            # redis-py 5.x deprecated close() in favor of aclose(); fall back
            # for older clients that don't have it.
            closer = getattr(self.redis, "aclose", None) or self.redis.close
            await closer()
            # Clear it too: leaving a closed client in place means anything
            # touching the cache after shutdown fails against a dead socket
            # instead of taking the well-tested "no cache" path.
            self.redis = None
            logger.info("Redis cache connection closed")


# Global instance
cache_service = CacheService()
