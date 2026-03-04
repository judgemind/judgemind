/**
 * REST API v1 plugin.
 *
 * Registers all /v1/* resource routes and wires up:
 *   - API key authentication (preHandler sets req.apiUser)
 *   - Rate limiting (unauthenticated: 30 req/min, authenticated: 500 req/min)
 *
 * Registered as a plain scoped Fastify plugin so the preHandler hook fires
 * only for /v1/* routes, leaving GraphQL and /health unaffected.
 */

import type { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import type { Pool } from 'pg';
import { extractApiUser, type ApiUser } from './api-auth';
import { casesRoutes } from './routes/cases';
import { judgesRoutes } from './routes/judges';
import { attorneysRoutes } from './routes/attorneys';
import { documentsRoutes } from './routes/documents';
import { rulingsRoutes } from './routes/rulings';
import { createClient, type RedisClientType } from 'redis';

// Augment FastifyRequest so route handlers can read req.apiUser.
declare module 'fastify' {
  interface FastifyRequest {
    apiUser: ApiUser | null;
  }
}

const ANON_LIMIT = 30; // requests per minute, unauthenticated
const AUTH_LIMIT = 500; // requests per minute, authenticated
const WINDOW_MS = 60 * 1000; // 1 minute

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
    redisClient = null;
    return null;
  }
}

/**
 * Checks (and increments) the rate-limit counter for a request.
 * Key: user-scoped for authenticated callers, IP-scoped for anonymous.
 * Returns true if the request should proceed.
 */
async function checkRateLimit(ip: string, apiUser: ApiUser | null): Promise<boolean> {
  const redis = await getRedis();
  if (!redis) return true; // fail open — consistent with login rate limiter

  const [key, limit] = apiUser
    ? [`rate:rest:api:${apiUser.id}`, AUTH_LIMIT]
    : [`rate:rest:ip:${ip}`, ANON_LIMIT];

  try {
    const count = await redis.incr(key);
    if (count === 1) {
      await redis.pExpire(key, WINDOW_MS);
    }
    return count <= limit;
  } catch {
    return true; // fail open on transient Redis errors
  }
}

export async function restApiPlugin(
  fastify: FastifyInstance,
  options: { pool: Pool },
): Promise<void> {
  const { pool } = options;

  // Declare the apiUser property on all requests within this plugin scope.
  fastify.decorateRequest('apiUser', null);

  // Authenticate and rate-limit every request routed through this plugin.
  fastify.addHook('preHandler', async (req: FastifyRequest, reply: FastifyReply) => {
    req.apiUser = await extractApiUser(req, pool);

    const ip =
      req.ip ??
      (typeof req.headers['x-forwarded-for'] === 'string'
        ? req.headers['x-forwarded-for']
        : 'unknown');

    const allowed = await checkRateLimit(ip, req.apiUser);
    if (!allowed) {
      await reply.status(429).send({
        statusCode: 429,
        error: 'Too Many Requests',
        message: req.apiUser
          ? 'Rate limit exceeded. Authenticated limit: 500 requests/minute.'
          : 'Rate limit exceeded. Unauthenticated limit: 30 requests/minute. Provide an X-API-Key header for higher limits.',
      });
    }
  });

  await fastify.register(casesRoutes, { pool });
  await fastify.register(judgesRoutes, { pool });
  await fastify.register(attorneysRoutes, { pool });
  await fastify.register(documentsRoutes, { pool });
  await fastify.register(rulingsRoutes, { pool });
}
