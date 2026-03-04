/**
 * REST API key authentication.
 *
 * Clients authenticate by sending their API key in one of two ways:
 *   Authorization: Bearer <api_key>
 *   X-API-Key: <api_key>
 *
 * API keys are stored in the `api_key` column of the `users` table.
 * Unauthenticated requests are allowed but subject to stricter rate limits.
 */

import type { Pool } from 'pg';
import type { FastifyRequest } from 'fastify';

export interface ApiUser {
  id: string;
  email: string;
  role: string;
}

export async function extractApiUser(
  req: FastifyRequest,
  pool: Pool,
): Promise<ApiUser | null> {
  const xApiKey = req.headers['x-api-key'];
  const authHeader = req.headers.authorization;

  const key =
    (typeof xApiKey === 'string' ? xApiKey : undefined) ??
    (typeof authHeader === 'string' ? authHeader.replace(/^Bearer\s+/i, '') : undefined);

  if (!key) return null;

  const { rows } = await pool.query<ApiUser>(
    'SELECT id, email, role FROM users WHERE api_key = $1 AND is_active = true',
    [key],
  );
  return rows[0] ?? null;
}
