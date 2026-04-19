import { createClient, type RedisClientType } from 'redis';

const WINDOW_SECONDS = 15 * 60; // 15 minutes
const MAX_ATTEMPTS = 10;

let redisClient: RedisClientType | null = null;

async function getRedis(): Promise<RedisClientType | null> {
  if (redisClient) return redisClient;
  try {
    redisClient = createClient({
      url: process.env.REDIS_URL ?? 'redis://localhost:6379',
      socket: { connectTimeout: 1000, reconnectStrategy: false },
    });
    await redisClient.connect();
    return redisClient;
  } catch {
    // Redis unavailable — fail open (allow request)
    redisClient = null;
    return null;
  }
}

/**
 * Check and increment the login attempt counter for an IP address.
 * Returns true if the request should be allowed, false if rate-limited.
 *
 * Set `DISABLE_LOGIN_RATE_LIMIT=1` to bypass the check — intended for the
 * integration test harness (tests/auth.integration.test.ts makes > 10 login
 * calls from the shared 127.0.0.1 IP, which otherwise trips the limit and
 * causes flaky failures against a locally-running Redis). See issue #2744.
 * The dedicated unit tests in tests/auth-rate-limit.unit.test.ts still
 * exercise the real rate-limit path by explicitly clearing this env var.
 */
export async function checkLoginRateLimit(ip: string): Promise<boolean> {
  if (process.env.DISABLE_LOGIN_RATE_LIMIT === '1') return true;

  const redis = await getRedis();
  if (!redis) return true; // fail open

  const key = `rate:login:${ip}`;
  const count = await redis.incr(key);
  if (count === 1) {
    await redis.expire(key, WINDOW_SECONDS);
  }
  return count <= MAX_ATTEMPTS;
}
