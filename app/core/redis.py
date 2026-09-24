import json
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import date
from app.core.config import settings

logger = logging.getLogger(__name__)

# Optional redis import to ensure code runs smoothly even if redis library is not yet installed in local dev
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    aioredis = None
    REDIS_AVAILABLE = False

_redis_client: Optional[Any] = None

def get_redis_client() -> Optional[Any]:
    global _redis_client
    if not REDIS_AVAILABLE:
        return None
    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=1.5,
                socket_connect_timeout=1.5
            )
        except Exception as e:
            logger.warning(f"Could not connect to Redis: {e}")
            _redis_client = None
    return _redis_client

async def close_redis():
    global _redis_client
    if _redis_client:
        try:
            await _redis_client.close()
        except Exception:
            pass
        _redis_client = None

# --- Subscription Tier State Caching ---

def _sub_key(app_id: str) -> str:
    return f"residence:sub:{app_id}"

def _quota_key(app_id: str) -> str:
    current_month = date.today().strftime("%Y-%m")
    return f"residence:quota:{app_id}:{current_month}"

def _visitor_token_key(app_id: str, code: str) -> str:
    return f"residence:visitor_token:{app_id}:{code}"

def _registration_token_key(app_id: str, code: str) -> str:
    return f"residence:reg_token:{app_id}:{code}"

async def get_cached_subscription_state(app_id: str) -> Optional[Dict[str, Any]]:
    client = get_redis_client()
    if not client:
        return None
    try:
        data = await client.get(_sub_key(app_id))
        if data:
            return json.loads(data)
    except Exception as e:
        logger.debug(f"Redis get_cached_subscription_state error: {e}")
    return None

async def set_cached_subscription_state(app_id: str, state: Dict[str, Any], ttl_seconds: int = 300) -> bool:
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.set(_sub_key(app_id), json.dumps(state), ex=ttl_seconds)
        return True
    except Exception as e:
        logger.debug(f"Redis set_cached_subscription_state error: {e}")
        return False

async def invalidate_subscription_cache(app_id: str) -> bool:
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.delete(_sub_key(app_id))
        return True
    except Exception as e:
        logger.debug(f"Redis invalidate_subscription_cache error: {e}")
        return False

# --- Atomic Lock-Free Quota Counter ---

async def increment_monthly_quota_redis(app_id: str, limit: Optional[int]) -> Tuple[bool, int]:
    """
    Atomically increments the monthly verification counter in Redis.
    Returns (is_allowed, current_count).
    If limit is None, unlimited verifications are allowed.
    """
    client = get_redis_client()
    if not client:
        return True, 0

    key = _quota_key(app_id)
    try:
        # Atomic INCR
        count = await client.incr(key)
        # Set 35 days TTL on first count of the month
        if count == 1:
            await client.expire(key, 35 * 86400)
            
        if limit is not None and count > limit:
            return False, count
        return True, count
    except Exception as e:
        logger.debug(f"Redis increment_monthly_quota error: {e}")
        # Graceful fallback to DB if Redis is unreachable
        return True, 0

# --- Visitor Token Fresh Cache (20-Minute TTL) ---

async def cache_visitor_token(
    app_id: str, 
    code: str, 
    token_data: Dict[str, Any], 
    ttl_seconds: int = 1200 # 20 minutes
) -> bool:
    """
    Cache a newly generated visitor token in Redis with a 20-minute TTL.
    Enables sub-millisecond fast-path reads when the visitor arrives promptly.
    """
    client = get_redis_client()
    if not client:
        return False
    try:
        key = _visitor_token_key(app_id, code)
        await client.set(key, json.dumps(token_data), ex=ttl_seconds)
        return True
    except Exception as e:
        logger.debug(f"Redis cache_visitor_token error: {e}")
        return False

async def consume_cached_visitor_token(app_id: str, code: str) -> Optional[Dict[str, Any]]:
    """
    Atomically fetches and deletes a cached visitor token using GETDEL (O(1)).
    Ensures the 1-time visitor pass cannot be reused from the cache.
    """
    client = get_redis_client()
    if not client:
        return None
    try:
        key = _visitor_token_key(app_id, code)
        data = await client.getdel(key)
        if data:
            return json.loads(data)
    except Exception as e:
        # Fallback for Redis versions that do not support GETDEL
        try:
            key = _visitor_token_key(app_id, code)
            data = await client.get(key)
            if data:
                await client.delete(key)
                return json.loads(data)
        except Exception:
            pass
        logger.debug(f"Redis consume_cached_visitor_token error: {e}")
    return None

# --- Registration Token Fresh Cache (20-Minute TTL) ---

async def cache_registration_token(
    app_id: str, 
    code: str, 
    token_data: Dict[str, Any], 
    ttl_seconds: int = 1200 # 20 minutes
) -> bool:
    client = get_redis_client()
    if not client:
        return False
    try:
        key = _registration_token_key(app_id, code)
        await client.set(key, json.dumps(token_data), ex=ttl_seconds)
        return True
    except Exception as e:
        logger.debug(f"Redis cache_registration_token error: {e}")
        return False

async def invalidate_cached_registration_token(app_id: str, code: str) -> bool:
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.delete(_registration_token_key(app_id, code))
        return True
    except Exception as e:
        logger.debug(f"Redis invalidate_cached_registration_token error: {e}")
        return False

# --- Book-Out (Exit) Token Fresh Cache (20-Minute TTL) ---

def _bookout_token_key(app_id: str, code: str) -> str:
    return f"residence:bookout_token:{app_id}:{code}"

async def cache_bookout_token(
    app_id: str, 
    code: str, 
    token_data: Dict[str, Any], 
    ttl_seconds: int = 1200 # 20 minutes
) -> bool:
    """Cache fresh book-out exit token in Redis for sub-millisecond exit barrier verification."""
    client = get_redis_client()
    if not client:
        return False
    try:
        key = _bookout_token_key(app_id, code)
        await client.set(key, json.dumps(token_data), ex=ttl_seconds)
        return True
    except Exception as e:
        logger.debug(f"Redis cache_bookout_token error: {e}")
        return False

async def consume_cached_bookout_token(app_id: str, code: str) -> Optional[Dict[str, Any]]:
    """
    Atomically retrieves and deletes the cached bookout token using GETDEL (O(1)).
    Ensures 1-time exit use without redis concurrency races.
    """
    client = get_redis_client()
    if not client:
        return None
    try:
        key = _bookout_token_key(app_id, code)
        data = await client.getdel(key)
        if data:
            return json.loads(data)
    except Exception as e:
        try:
            key = _bookout_token_key(app_id, code)
            data = await client.get(key)
            if data:
                await client.delete(key)
                return json.loads(data)
        except Exception:
            pass
        logger.debug(f"Redis consume_cached_bookout_token error: {e}")
    return None

# --- User Session & JWT Authentication Cache ---

def _user_cache_key(app_id: str, email: str) -> str:
    return f"residence:user:{app_id}:{email}"

def _estate_cache_key(app_id: str) -> str:
    return f"residence:estate_valid:{app_id}"

def _token_blacklist_key(token_hash: str) -> str:
    return f"residence:token_blacklist:{token_hash}"

async def get_cached_user(app_id: str, email: str) -> Optional[Dict[str, Any]]:
    """Fetch cached user profile from Redis to avoid PostgreSQL queries on every API request."""
    client = get_redis_client()
    if not client:
        return None
    try:
        data = await client.get(_user_cache_key(app_id, email))
        if data:
            return json.loads(data)
    except Exception as e:
        logger.debug(f"Redis get_cached_user error: {e}")
    return None

async def set_cached_user(
    app_id: str, 
    email: str, 
    user_data: Dict[str, Any], 
    ttl_seconds: int = 1800 # 30 minutes
) -> bool:
    """Cache user profile in Redis with TTL matching JWT active session duration."""
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.set(_user_cache_key(app_id, email), json.dumps(user_data), ex=ttl_seconds)
        return True
    except Exception as e:
        logger.debug(f"Redis set_cached_user error: {e}")
        return False

async def invalidate_cached_user(app_id: str, email: str) -> bool:
    """Invalidate cached user profile on user update, revocation, or role change."""
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.delete(_user_cache_key(app_id, email))
        return True
    except Exception as e:
        logger.debug(f"Redis invalidate_cached_user error: {e}")
        return False

# --- Tenant (Estate) Validation Cache ---

async def get_cached_estate_validity(app_id: str) -> Optional[bool]:
    """Check if app_id is verified and cached in Redis."""
    client = get_redis_client()
    if not client:
        return None
    try:
        val = await client.get(_estate_cache_key(app_id))
        if val is not None:
            return val == "1"
    except Exception as e:
        logger.debug(f"Redis get_cached_estate_validity error: {e}")
    return None

async def set_cached_estate_validity(app_id: str, is_valid: bool = True, ttl_seconds: int = 86400) -> bool:
    """Cache estate app_id validity for 24 hours to eliminate tenant DB lookup on every request."""
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.set(_estate_cache_key(app_id), "1" if is_valid else "0", ex=ttl_seconds)
        return True
    except Exception as e:
        logger.debug(f"Redis set_cached_estate_validity error: {e}")
        return False

# --- JWT Token Blacklisting / Revocation ---

import hashlib

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

async def is_token_blacklisted(token: str) -> bool:
    """Check if token is blacklisted in Redis (e.g. after logout or revocation)."""
    client = get_redis_client()
    if not client:
        return False
    try:
        val = await client.get(_token_blacklist_key(_hash_token(token)))
        return val is not None
    except Exception as e:
        logger.debug(f"Redis is_token_blacklisted error: {e}")
        return False

async def blacklist_token(token: str, ttl_seconds: int = 3600) -> bool:
    """Blacklist a JWT token in Redis until its expiration."""
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.set(_token_blacklist_key(_hash_token(token)), "blacklisted", ex=ttl_seconds)
        return True
    except Exception as e:
        logger.debug(f"Redis blacklist_token error: {e}")
        return False

# --- Verification Rate-Limiting (Anti-Brute-Force for 4-Digit Codes) ---

def _verify_fail_key(app_id: str, client_ip: str) -> str:
    return f"residence:ratelimit:fail:{app_id}:{client_ip}"

def _verify_block_key(app_id: str, client_ip: str) -> str:
    return f"residence:ratelimit:blocked:{app_id}:{client_ip}"

async def check_verification_blocked(app_id: str, client_ip: str) -> Tuple[bool, int]:
    """
    Checks if a client/gate is temporarily blocked from verification due to excessive failures.
    Returns (is_blocked, remaining_seconds).
    """
    client = get_redis_client()
    if not client:
        return False, 0
    try:
        block_key = _verify_block_key(app_id, client_ip)
        ttl = await client.ttl(block_key)
        if ttl > 0:
            return True, ttl
        return False, 0
    except Exception as e:
        logger.debug(f"Redis check_verification_blocked error: {e}")
        return False, 0

async def record_verification_failure(
    app_id: str, 
    client_ip: str, 
    max_failures: int = 5, 
    window_seconds: int = 60, 
    block_seconds: int = 60
) -> Tuple[int, bool, int]:
    """
    Records a failed verification attempt.
    If failures reach max_failures (e.g., 5), a block key is set for block_seconds.
    Returns (current_failures, is_blocked, remaining_seconds).
    """
    client = get_redis_client()
    if not client:
        return 1, False, 0
    try:
        fail_key = _verify_fail_key(app_id, client_ip)
        block_key = _verify_block_key(app_id, client_ip)

        failures = await client.incr(fail_key)
        if failures == 1:
            await client.expire(fail_key, window_seconds)

        if failures >= max_failures:
            await client.set(block_key, "1", ex=block_seconds)
            await client.delete(fail_key)
            return failures, True, block_seconds

        return failures, False, 0
    except Exception as e:
        logger.debug(f"Redis record_verification_failure error: {e}")
        return 1, False, 0

async def reset_verification_failures(app_id: str, client_ip: str) -> bool:
    """Resets the failure counter on a successful verification."""
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.delete(_verify_fail_key(app_id, client_ip))
        return True
    except Exception as e:
        logger.debug(f"Redis reset_verification_failures error: {e}")
        return False

# --- Authentication Rate-Limiting (Anti-Brute-Force & Credential Stuffing) ---

def _login_fail_key(identifier: str) -> str:
    return f"residence:ratelimit:login:fail:{identifier}"

def _login_block_key(identifier: str) -> str:
    return f"residence:ratelimit:login:blocked:{identifier}"

async def check_login_blocked(identifier: str) -> Tuple[bool, int]:
    """
    Checks if an IP or user identifier is temporarily blocked from login attempts.
    Returns (is_blocked, remaining_seconds).
    """
    client = get_redis_client()
    if not client:
        return False, 0
    try:
        block_key = _login_block_key(identifier)
        ttl = await client.ttl(block_key)
        if ttl > 0:
            return True, ttl
        return False, 0
    except Exception as e:
        logger.debug(f"Redis check_login_blocked error: {e}")
        return False, 0

async def record_login_failure(
    identifier: str,
    max_failures: int = 5,
    window_seconds: int = 60,
    block_seconds: int = 60
) -> Tuple[int, bool, int]:
    """
    Records a failed login attempt.
    If failures exceed max_failures within window_seconds, sets temporary block for block_seconds.
    Returns (current_failures, is_blocked, remaining_seconds).
    """
    client = get_redis_client()
    if not client:
        return 1, False, 0
    try:
        fail_key = _login_fail_key(identifier)
        block_key = _login_block_key(identifier)

        failures = await client.incr(fail_key)
        if failures == 1:
            await client.expire(fail_key, window_seconds)

        if failures >= max_failures:
            await client.set(block_key, "1", ex=block_seconds)
            await client.delete(fail_key)
            return failures, True, block_seconds

        return failures, False, 0
    except Exception as e:
        logger.debug(f"Redis record_login_failure error: {e}")
        return 1, False, 0

async def reset_login_failures(identifier: str) -> bool:
    """Clears failed login attempt counter upon successful authentication."""
    client = get_redis_client()
    if not client:
        return False
    try:
        await client.delete(_login_fail_key(identifier))
        return True
    except Exception as e:
        logger.debug(f"Redis reset_login_failures error: {e}")
        return False

# --- Generic Sliding Window Rate-Limiter (Token Creation, API Throttling) ---

async def check_rate_limit(
    key_prefix: str,
    identifier: str,
    max_requests: int = 10,
    window_seconds: int = 60
) -> Tuple[bool, int, int]:
    """
    Generic atomic sliding window counter rate-limiter backed by Redis.
    Returns (is_allowed, current_count, retry_after_seconds).
    Fails open (returns True) if Redis is temporarily unreachable.
    """
    client = get_redis_client()
    if not client:
        return True, 1, 0

    key = f"residence:ratelimit:{key_prefix}:{identifier}"
    try:
        current = await client.incr(key)
        if current == 1:
            await client.expire(key, window_seconds)
            return True, current, 0

        if current > max_requests:
            ttl = await client.ttl(key)
            ttl = max(1, ttl) if ttl > 0 else window_seconds
            return False, current, ttl

        return True, current, 0
    except Exception as e:
        logger.debug(f"Redis check_rate_limit error: {e}")
        return True, 1, 0




