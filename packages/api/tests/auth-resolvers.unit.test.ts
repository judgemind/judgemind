/**
 * Unit tests for auth-resolvers.ts — targets resolver branches that are hard
 * to reach via integration tests (e.g. Google OAuth with mocked fetch,
 * module-scope variable gating).
 *
 * These tests import the resolvers directly and call them with mock
 * Pool/Reply objects, so they do NOT require a running PostgreSQL.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { QueryResult } from 'pg';
import type { FastifyReply } from 'fastify';

// ---------------------------------------------------------------------------
// Mock google-auth-library
// ---------------------------------------------------------------------------

// verifyIdToken mock — tests override this via vi.mocked(mockVerifyIdToken)
const mockVerifyIdToken = vi.fn();
const mockGetPayload = vi.fn();

vi.mock('google-auth-library', () => {
  return {
    OAuth2Client: vi.fn().mockImplementation(() => ({
      verifyIdToken: mockVerifyIdToken,
    })),
  };
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A minimal mock Reply that captures set-cookie headers. */
function mockReply(): FastifyReply & { _headers: Record<string, string> } {
  const headers: Record<string, string> = {};
  return {
    _headers: headers,
    header: vi.fn((key: string, val: string) => {
      headers[key.toLowerCase()] = val;
    }),
  } as unknown as FastifyReply & { _headers: Record<string, string> };
}

/** Build a mock Pool whose .query() resolves with controlled results. */
function mockPool(
  queryResults: Array<Partial<QueryResult>>,
): Pool {
  let callIndex = 0;
  return {
    query: vi.fn(async () => {
      const result = queryResults[callIndex] ?? { rows: [], rowCount: 0 };
      callIndex++;
      return result;
    }),
  } as unknown as Pool;
}

/** Encode a fake Google ID token (unsigned JWT-like base64 payload). */
function fakeGoogleIdToken(payload: Record<string, unknown>): string {
  const header = Buffer.from(JSON.stringify({ alg: 'RS256' })).toString('base64');
  const body = Buffer.from(JSON.stringify(payload)).toString('base64');
  return `${header}.${body}.fakesig`;
}

/** Create a mock ticket returned by verifyIdToken. */
function makeTicket(payload: Record<string, unknown> | null) {
  mockGetPayload.mockReturnValue(payload);
  return { getPayload: mockGetPayload };
}

// ---------------------------------------------------------------------------
// Tests for completeGoogleAuth
// ---------------------------------------------------------------------------

// We need to set module-scope constants BEFORE importing auth-resolvers,
// because GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are captured at module load.
// Use dynamic import after setting env vars.

describe('completeGoogleAuth — unit tests', () => {
  let originalFetch: typeof globalThis.fetch;
  let originalClientId: string | undefined;
  let originalClientSecret: string | undefined;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    originalClientId = process.env.GOOGLE_CLIENT_ID;
    originalClientSecret = process.env.GOOGLE_CLIENT_SECRET;
    mockVerifyIdToken.mockReset();
    mockGetPayload.mockReset();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    process.env.GOOGLE_CLIENT_ID = originalClientId;
    process.env.GOOGLE_CLIENT_SECRET = originalClientSecret;
    vi.resetModules();
  });

  async function loadResolvers() {
    // Dynamic import to pick up current env vars as module-scope constants
    const mod = await import('../src/graphql/auth-resolvers');
    return mod.authResolvers;
  }

  it('creates a new user when no existing user matches Google id or email', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    const googlePayload = {
      sub: 'google-123',
      email: 'newuser@gmail.com',
      email_verified: true,
      name: 'New User',
    };

    // Mock fetch for Google token exchange — verifyIdToken mock handles payload extraction
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id_token: fakeGoogleIdToken(googlePayload) }),
    } as Response);

    mockVerifyIdToken.mockResolvedValue(makeTicket(googlePayload));

    const newUser = {
      id: 'user-uuid-1',
      email: 'newuser@gmail.com',
      display_name: 'New User',
      email_verified: true,
      role: 'user',
      created_at: new Date().toISOString(),
      google_id: 'google-123',
    };

    // Query sequence:
    // 1. SELECT * FROM users WHERE google_id = $1 OR email = $2 → empty (no existing user)
    // 2. INSERT INTO users → returns new user
    // 3. INSERT INTO refresh_tokens → void
    const pool = mockPool([
      { rows: [], rowCount: 0 },
      { rows: [newUser], rowCount: 1 },
      { rows: [], rowCount: 1 },
    ]);

    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    const result = await resolvers.Mutation.completeGoogleAuth(
      {},
      { code: 'auth-code-123' },
      ctx,
    );

    expect(result.accessToken).toBeTruthy();
    expect(result.user.email).toBe('newuser@gmail.com');
    expect(result.user.displayName).toBe('New User');
    expect(globalThis.fetch).toHaveBeenCalledOnce();
    // Verify verifyIdToken was called (not raw base64 decode)
    expect(mockVerifyIdToken).toHaveBeenCalledOnce();
    // Verify refresh token cookie was set
    expect(reply.header).toHaveBeenCalled();
  });

  it('links google_id to existing user found by email', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    const googlePayload = {
      sub: 'google-456',
      email: 'existing@gmail.com',
      email_verified: true,
      name: 'Existing User',
    };

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id_token: fakeGoogleIdToken(googlePayload) }),
    } as Response);

    mockVerifyIdToken.mockResolvedValue(makeTicket(googlePayload));

    const existingUser = {
      id: 'user-uuid-2',
      email: 'existing@gmail.com',
      display_name: 'Existing User',
      email_verified: false,
      role: 'user',
      created_at: new Date().toISOString(),
      google_id: null, // Not yet linked
    };

    // Query sequence:
    // 1. SELECT → existing user (found by email, no google_id)
    // 2. UPDATE users SET google_id (link it)
    // 3. UPDATE users SET email_verified = true
    // 4. UPDATE users SET last_login_at = NOW()
    // 5. INSERT INTO refresh_tokens
    const pool = mockPool([
      { rows: [existingUser], rowCount: 1 },
      { rows: [], rowCount: 1 },
      { rows: [], rowCount: 1 },
      { rows: [], rowCount: 1 },
      { rows: [], rowCount: 1 },
    ]);

    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    const result = await resolvers.Mutation.completeGoogleAuth(
      {},
      { code: 'auth-code-456' },
      ctx,
    );

    expect(result.accessToken).toBeTruthy();
    expect(result.user.email).toBe('existing@gmail.com');
    // Should have called update for google_id
    expect(pool.query).toHaveBeenCalledWith(
      'UPDATE users SET google_id = $1 WHERE id = $2',
      ['google-456', 'user-uuid-2'],
    );
  });

  it('returns existing user when found by google_id (already linked)', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    const googlePayload = {
      sub: 'google-789',
      email: 'linked@gmail.com',
      email_verified: true,
      name: 'Linked User',
    };

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id_token: fakeGoogleIdToken(googlePayload) }),
    } as Response);

    mockVerifyIdToken.mockResolvedValue(makeTicket(googlePayload));

    const existingUser = {
      id: 'user-uuid-3',
      email: 'linked@gmail.com',
      display_name: 'Linked User',
      email_verified: true,
      role: 'user',
      created_at: new Date().toISOString(),
      google_id: 'google-789', // Already linked
    };

    // Query sequence:
    // 1. SELECT → existing user (already has google_id and email_verified)
    // 2. UPDATE last_login_at
    // 3. INSERT INTO refresh_tokens
    const pool = mockPool([
      { rows: [existingUser], rowCount: 1 },
      { rows: [], rowCount: 1 },
      { rows: [], rowCount: 1 },
    ]);

    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    const result = await resolvers.Mutation.completeGoogleAuth(
      {},
      { code: 'auth-code-789' },
      ctx,
    );

    expect(result.accessToken).toBeTruthy();
    expect(result.user.email).toBe('linked@gmail.com');
    // Should NOT have called update for google_id (already set)
    const queryCalls = (pool.query as ReturnType<typeof vi.fn>).mock.calls;
    const googleIdUpdateCalls = queryCalls.filter(
      (call: unknown[]) => typeof call[0] === 'string' && call[0].includes('SET google_id'),
    );
    expect(googleIdUpdateCalls).toHaveLength(0);
  });

  it('throws when Google token exchange fails (non-ok response)', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ error: 'invalid_grant' }),
    } as Response);

    const pool = mockPool([]);
    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    await expect(
      resolvers.Mutation.completeGoogleAuth({}, { code: 'bad-code' }, ctx),
    ).rejects.toThrow('Failed to exchange Google authorization code');
  });

  it('throws when no id_token in Google response', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: 'some-token' }), // no id_token
    } as Response);

    const pool = mockPool([]);
    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    await expect(
      resolvers.Mutation.completeGoogleAuth({}, { code: 'auth-code' }, ctx),
    ).rejects.toThrow('No ID token received from Google');
  });

  it('throws when Google OAuth is not configured', async () => {
    process.env.GOOGLE_CLIENT_ID = '';
    process.env.GOOGLE_CLIENT_SECRET = '';
    const resolvers = await loadResolvers();

    const pool = mockPool([]);
    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    await expect(
      resolvers.Mutation.completeGoogleAuth({}, { code: 'auth-code' }, ctx),
    ).rejects.toThrow('Google OAuth is not configured');
  });

  it('throws when email_verified is false (AC #2)', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    const googlePayload = {
      sub: 'google-unverified',
      email: 'unverified@gmail.com',
      email_verified: false,
      name: 'Unverified User',
    };

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id_token: fakeGoogleIdToken(googlePayload) }),
    } as Response);

    mockVerifyIdToken.mockResolvedValue(makeTicket(googlePayload));

    const pool = mockPool([]);
    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    await expect(
      resolvers.Mutation.completeGoogleAuth({}, { code: 'auth-code-unverified' }, ctx),
    ).rejects.toThrow('Google account email is not verified');
  });

  it('throws when verifyIdToken rejects with audience error (AC #3)', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id_token: 'header.payload.sig' }),
    } as Response);

    mockVerifyIdToken.mockRejectedValue(new Error('Token audience mismatch'));

    const pool = mockPool([]);
    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    await expect(
      resolvers.Mutation.completeGoogleAuth({}, { code: 'auth-code-bad-aud' }, ctx),
    ).rejects.toThrow('Invalid Google ID token');
  });

  it('throws when verifyIdToken returns null payload', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';
    const resolvers = await loadResolvers();

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id_token: 'header.payload.sig' }),
    } as Response);

    mockVerifyIdToken.mockResolvedValue(makeTicket(null));

    const pool = mockPool([]);
    const reply = mockReply();
    const ctx = { pool, reply, user: null, ip: '127.0.0.1', cookieHeader: '' };

    await expect(
      resolvers.Mutation.completeGoogleAuth({}, { code: 'auth-code-null-payload' }, ctx),
    ).rejects.toThrow('Invalid Google ID token');
  });
});

// ---------------------------------------------------------------------------
// Tests for initiateGoogleAuth
// ---------------------------------------------------------------------------

describe('initiateGoogleAuth — unit tests', () => {
  let originalClientId: string | undefined;
  let originalClientSecret: string | undefined;

  beforeEach(() => {
    originalClientId = process.env.GOOGLE_CLIENT_ID;
    originalClientSecret = process.env.GOOGLE_CLIENT_SECRET;
  });

  afterEach(() => {
    process.env.GOOGLE_CLIENT_ID = originalClientId;
    process.env.GOOGLE_CLIENT_SECRET = originalClientSecret;
    vi.resetModules();
  });

  it('returns Google OAuth URL with correct parameters', async () => {
    process.env.GOOGLE_CLIENT_ID = 'test-client-id';
    process.env.GOOGLE_CLIENT_SECRET = 'test-secret';
    const mod = await import('../src/graphql/auth-resolvers');
    const resolvers = mod.authResolvers;

    const url = resolvers.Mutation.initiateGoogleAuth();
    expect(url).toContain('https://accounts.google.com/o/oauth2/v2/auth');
    expect(url).toContain('client_id=test-client-id');
    expect(url).toContain('response_type=code');
    expect(url).toContain('scope=openid+email+profile');
  });

  it('throws when Google OAuth is not configured', async () => {
    process.env.GOOGLE_CLIENT_ID = '';
    const mod = await import('../src/graphql/auth-resolvers');
    const resolvers = mod.authResolvers;

    expect(() => resolvers.Mutation.initiateGoogleAuth()).toThrow(
      'Google OAuth is not configured',
    );
  });
});

// ---------------------------------------------------------------------------
// Tests for User type resolvers
// ---------------------------------------------------------------------------

describe('User type resolvers — unit tests', () => {
  it('resolves emailVerified from snake_case (DB row)', async () => {
    const mod = await import('../src/graphql/auth-resolvers');
    const resolvers = mod.authResolvers;

    const row = { email_verified: true };
    expect(resolvers.User.emailVerified(row)).toBe(true);
  });

  it('resolves emailVerified from camelCase (already transformed)', async () => {
    const mod = await import('../src/graphql/auth-resolvers');
    const resolvers = mod.authResolvers;

    const row = { emailVerified: false };
    expect(resolvers.User.emailVerified(row)).toBe(false);
  });

  it('resolves displayName from snake_case', async () => {
    const mod = await import('../src/graphql/auth-resolvers');
    const resolvers = mod.authResolvers;

    const row = { display_name: 'Jane Doe' };
    expect(resolvers.User.displayName(row)).toBe('Jane Doe');
  });

  it('resolves createdAt from snake_case', async () => {
    const mod = await import('../src/graphql/auth-resolvers');
    const resolvers = mod.authResolvers;

    const ts = '2024-01-01T00:00:00Z';
    const row = { created_at: ts };
    expect(resolvers.User.createdAt(row)).toBe(ts);
  });
});
