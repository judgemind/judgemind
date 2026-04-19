import { describe, it, expect, vi } from 'vitest';
import type { FastifyRequest } from 'fastify';
import type { Pool, QueryResult } from 'pg';

// Mock verifyAccessToken via vi.hoisted so the mock variable is available
// before vi.mock runs (vi.mock is hoisted by vitest).
const mockVerifyAccessToken = vi.hoisted(() => vi.fn());

vi.mock('../src/auth/tokens', () => ({
  verifyAccessToken: mockVerifyAccessToken,
}));

import { extractUser } from '../src/auth/middleware';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mockRequest(authorization?: string): FastifyRequest {
  return {
    headers: { authorization },
  } as unknown as FastifyRequest;
}

function mockPool(
  rows: Array<{ id: string; email: string; role: string; is_admin?: boolean }>,
): Pool {
  return {
    query: vi.fn().mockResolvedValue({
      rows,
      command: 'SELECT',
      rowCount: rows.length,
      oid: 0,
      fields: [],
    } as QueryResult),
  } as unknown as Pool;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('extractUser', () => {
  it('returns null when no Authorization header is present', async () => {
    const req = mockRequest(undefined);
    const pool = mockPool([]);
    const result = await extractUser(req, pool);
    expect(result).toBeNull();
  });

  it('returns null when Authorization header does not start with Bearer', async () => {
    const req = mockRequest('Basic abc123');
    const pool = mockPool([]);
    const result = await extractUser(req, pool);
    expect(result).toBeNull();
  });

  it('returns null when Authorization header is just "Bearer" with no token', async () => {
    // "Bearer " has no token after it — header.slice(7) gives ""
    const req = mockRequest('Bearer ');
    const pool = mockPool([]);
    mockVerifyAccessToken.mockImplementation(() => {
      throw new Error('invalid token');
    });
    const result = await extractUser(req, pool);
    expect(result).toBeNull();
  });

  it('returns the user when token is valid and user exists', async () => {
    const row = { id: 'user-1', email: 'alice@example.com', role: 'user', is_admin: false };
    mockVerifyAccessToken.mockReturnValue({ sub: 'user-1' });
    const pool = mockPool([row]);

    const req = mockRequest('Bearer valid-token');
    const result = await extractUser(req, pool);

    expect(result).toEqual({
      id: 'user-1',
      email: 'alice@example.com',
      role: 'user',
      isAdmin: false,
    });
    expect(mockVerifyAccessToken).toHaveBeenCalledWith('valid-token');
    expect(pool.query).toHaveBeenCalledWith(
      'SELECT id, email, role, is_admin FROM users WHERE id = $1 AND is_active = true',
      ['user-1'],
    );
  });

  it('returns isAdmin=true when the DB row has is_admin=true', async () => {
    const row = {
      id: 'admin-1',
      email: 'admin@example.com',
      role: 'user',
      is_admin: true,
    };
    mockVerifyAccessToken.mockReturnValue({ sub: 'admin-1' });
    const pool = mockPool([row]);

    const req = mockRequest('Bearer admin-token');
    const result = await extractUser(req, pool);

    expect(result).toEqual({
      id: 'admin-1',
      email: 'admin@example.com',
      role: 'user',
      isAdmin: true,
    });
  });

  it('returns null when token is valid but user does not exist in DB', async () => {
    mockVerifyAccessToken.mockReturnValue({ sub: 'deleted-user' });
    const pool = mockPool([]); // no rows returned

    const req = mockRequest('Bearer valid-token');
    const result = await extractUser(req, pool);

    expect(result).toBeNull();
  });

  it('returns null when verifyAccessToken throws (expired token)', async () => {
    mockVerifyAccessToken.mockImplementation(() => {
      throw new Error('jwt expired');
    });
    const pool = mockPool([]);

    const req = mockRequest('Bearer expired-token');
    const result = await extractUser(req, pool);

    expect(result).toBeNull();
    // Pool should not be queried if token verification fails
    expect(pool.query).not.toHaveBeenCalled();
  });

  it('returns null when verifyAccessToken throws (malformed token)', async () => {
    mockVerifyAccessToken.mockImplementation(() => {
      throw new Error('jwt malformed');
    });
    const pool = mockPool([]);

    const req = mockRequest('Bearer not-a-jwt');
    const result = await extractUser(req, pool);

    expect(result).toBeNull();
    expect(pool.query).not.toHaveBeenCalled();
  });
});
