import type { FastifyRequest } from 'fastify';
import type { Pool } from 'pg';
import { verifyAccessToken } from './tokens';

export interface AuthUser {
  id: string;
  email: string;
  role: string;
}

/**
 * Extract the authenticated user from the Authorization header.
 * Returns null if no token is present or the token is invalid/expired.
 * Does NOT throw — unauthenticated requests are allowed through so
 * individual resolvers can decide whether to require auth.
 */
export async function extractUser(
  req: FastifyRequest,
  pool: Pool,
): Promise<AuthUser | null> {
  const header = req.headers.authorization;
  if (!header?.startsWith('Bearer ')) return null;

  const token = header.slice(7);
  try {
    const payload = verifyAccessToken(token);
    // Verify user still exists and is active
    const { rows } = await pool.query<{
      id: string;
      email: string;
      role: string;
    }>(
      'SELECT id, email, role FROM users WHERE id = $1 AND is_active = true',
      [payload.sub],
    );
    const row = rows[0];
    if (!row) return null;
    return { id: row.id, email: row.email, role: row.role };
  } catch {
    return null;
  }
}
