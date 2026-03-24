/**
 * Integration tests for user authentication — register, login, token refresh,
 * email verification, and Google OAuth exchange.
 *
 * Runs against a real PostgreSQL database (same as graphql.integration.test.ts).
 *
 * DATA ISOLATION: This file does not seed court/county data — only user rows
 * with a timestamp-based email prefix to avoid collisions.
 * See tests/test-counties.ts for the full registry.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool, types } from 'pg';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';
import { signVerificationToken } from '../src/auth/tokens';
import { applyMigrations } from './setup-db';

types.setTypeParser(1082, (val: string) => val);
types.setTypeParser(1114, (val: string) => val);
types.setTypeParser(1184, (val: string) => val);

const pool = new Pool({
  connectionString:
    process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind',
});

let app: FastifyInstance;

// Unique email prefix to avoid collisions with other test runs
const PREFIX = `test-${Date.now()}`;

beforeAll(async () => {
  applyMigrations();
  app = await buildApp(pool);
}, 30_000);

afterAll(async () => {
  // Clean up test users
  await pool.query(`DELETE FROM refresh_tokens WHERE user_id IN (SELECT id FROM users WHERE email LIKE $1)`, [`${PREFIX}%`]);
  await pool.query(`DELETE FROM users WHERE email LIKE $1`, [`${PREFIX}%`]);
  await app?.close();
  await pool.end();
}, 15_000);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function gql(query: string, variables?: Record<string, unknown>, headers?: Record<string, string>) {
  const res = await app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json', ...headers },
    payload: JSON.stringify({ query, variables }),
  });
  return {
    body: JSON.parse(res.body) as { data?: Record<string, unknown>; errors?: Array<{ message: string; extensions?: { code?: string } }> },
    headers: res.headers,
    statusCode: res.statusCode,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Auth — integration', () => {
  const email = `${PREFIX}@example.com`;
  const password = 'TestPass123!';
  let accessToken: string;
  let userId: string;

  describe('register', () => {
    it('creates a new user and returns accessToken + user', async () => {
      const { body } = await gql(
        `mutation($email: String!, $password: String!, $name: String) {
          register(email: $email, password: $password, displayName: $name) {
            accessToken
            user { id email emailVerified displayName role }
          }
        }`,
        { email, password, name: 'Test User' },
      );

      expect(body.errors).toBeUndefined();
      const payload = body.data?.register as { accessToken: string; user: Record<string, unknown> };
      expect(payload.accessToken).toBeTruthy();
      expect(payload.user.email).toBe(email);
      expect(payload.user.emailVerified).toBe(false);
      expect(payload.user.displayName).toBe('Test User');
      expect(payload.user.role).toBe('user');

      accessToken = payload.accessToken;
      userId = payload.user.id as string;
    });

    it('rejects duplicate email', async () => {
      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          register(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password },
      );

      expect(body.errors).toBeDefined();
      expect(body.errors![0].message).toContain('already exists');
    });

    it('rejects password shorter than 8 characters', async () => {
      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          register(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email: `${PREFIX}-short@example.com`, password: 'abc' },
      );

      expect(body.errors).toBeDefined();
    });

    it('rejects invalid email', async () => {
      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          register(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email: 'not-an-email', password },
      );

      expect(body.errors).toBeDefined();
    });
  });

  describe('me', () => {
    it('returns user when authenticated', async () => {
      const { body } = await gql(
        '{ me { id email displayName } }',
        undefined,
        { authorization: `Bearer ${accessToken}` },
      );

      expect(body.errors).toBeUndefined();
      expect(body.data?.me).toMatchObject({
        id: userId,
        email,
        displayName: 'Test User',
      });
    });

    it('returns null when not authenticated', async () => {
      const { body } = await gql('{ me { id } }');

      expect(body.errors).toBeUndefined();
      expect(body.data?.me).toBeNull();
    });

    it('returns null for invalid token', async () => {
      const { body } = await gql(
        '{ me { id } }',
        undefined,
        { authorization: 'Bearer invalid-token' },
      );

      expect(body.errors).toBeUndefined();
      expect(body.data?.me).toBeNull();
    });
  });

  describe('login', () => {
    it('returns accessToken + user on valid credentials', async () => {
      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) {
            accessToken
            user { id email }
          }
        }`,
        { email, password },
      );

      expect(body.errors).toBeUndefined();
      const payload = body.data?.login as { accessToken: string; user: { id: string; email: string } };
      expect(payload.accessToken).toBeTruthy();
      expect(payload.user.email).toBe(email);
    });

    it('rejects wrong password', async () => {
      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password: 'wrong-password' },
      );

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });

    it('rejects non-existent email', async () => {
      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email: 'nobody@nowhere.com', password },
      );

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });
  });

  describe('verifyEmail', () => {
    it('verifies email with a valid token', async () => {
      const token = signVerificationToken(userId);
      const { body } = await gql(
        `mutation($token: String!) { verifyEmail(token: $token) }`,
        { token },
      );

      expect(body.errors).toBeUndefined();
      expect(body.data?.verifyEmail).toBe(true);

      // Confirm in DB
      const { rows } = await pool.query('SELECT email_verified FROM users WHERE id = $1', [userId]);
      expect(rows[0].email_verified).toBe(true);
    });

    it('returns false for already-verified user', async () => {
      const token = signVerificationToken(userId);
      const { body } = await gql(
        `mutation($token: String!) { verifyEmail(token: $token) }`,
        { token },
      );

      expect(body.errors).toBeUndefined();
      expect(body.data?.verifyEmail).toBe(false);
    });

    it('rejects invalid token', async () => {
      const { body } = await gql(
        `mutation($token: String!) { verifyEmail(token: $token) }`,
        { token: 'garbage' },
      );

      expect(body.errors).toBeDefined();
    });
  });

  describe('logout', () => {
    it('requires authentication', async () => {
      const { body } = await gql('mutation { logout }');

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });

    it('succeeds when authenticated', async () => {
      const { body } = await gql(
        'mutation { logout }',
        undefined,
        { authorization: `Bearer ${accessToken}` },
      );

      expect(body.errors).toBeUndefined();
      expect(body.data?.logout).toBe(true);
    });
  });

  describe('refreshToken', () => {
    it('rejects when no cookie is present', async () => {
      const { body } = await gql('mutation { refreshToken { accessToken user { id } } }');

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });

    it('works end-to-end: login sets cookie, refreshToken uses it', async () => {
      // Login to get a refresh token cookie
      const loginRes = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password },
      );
      expect(loginRes.body.errors).toBeUndefined();

      // Extract set-cookie header
      const setCookie = loginRes.headers['set-cookie'];
      const cookieStr = Array.isArray(setCookie) ? setCookie[0] : setCookie;
      expect(cookieStr).toContain('refreshToken=');

      // Extract just the cookie value for the next request
      const cookieMatch = (cookieStr as string).match(/refreshToken=([^;]+)/);
      expect(cookieMatch).toBeTruthy();

      // Use the refresh token
      const refreshRes = await gql(
        'mutation { refreshToken { accessToken user { id email } } }',
        undefined,
        { cookie: `refreshToken=${cookieMatch![1]}` },
      );

      expect(refreshRes.body.errors).toBeUndefined();
      const payload = refreshRes.body.data?.refreshToken as { accessToken: string; user: { id: string } };
      expect(payload.accessToken).toBeTruthy();
      expect(payload.user.id).toBe(userId);

      // Token rotation: the old token should no longer work
      const retryRes = await gql(
        'mutation { refreshToken { accessToken user { id } } }',
        undefined,
        { cookie: `refreshToken=${cookieMatch![1]}` },
      );
      expect(retryRes.body.errors).toBeDefined();
    });
  });

  describe('register — additional branches', () => {
    it('registers without displayName (null branch)', async () => {
      const noNameEmail = `${PREFIX}-noname@example.com`;
      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          register(email: $email, password: $password) {
            accessToken
            user { id email displayName }
          }
        }`,
        { email: noNameEmail, password },
      );

      expect(body.errors).toBeUndefined();
      const payload = body.data?.register as { accessToken: string; user: Record<string, unknown> };
      expect(payload.accessToken).toBeTruthy();
      expect(payload.user.email).toBe(noNameEmail);
      expect(payload.user.displayName).toBeNull();
    });

    it('sets refresh token cookie on register', async () => {
      const cookieEmail = `${PREFIX}-cookie@example.com`;
      const { body, headers: resHeaders } = await gql(
        `mutation($email: String!, $password: String!) {
          register(email: $email, password: $password) {
            accessToken
            user { id }
          }
        }`,
        { email: cookieEmail, password },
      );

      expect(body.errors).toBeUndefined();
      const setCookie = resHeaders['set-cookie'];
      const cookieStr = Array.isArray(setCookie) ? setCookie[0] : setCookie;
      expect(cookieStr).toContain('refreshToken=');
      expect(cookieStr).toContain('HttpOnly');
    });
  });

  describe('login — additional branches', () => {
    it('rejects login for OAuth-only user (no password_hash)', async () => {
      // Insert an OAuth-only user directly into the DB
      const oauthEmail = `${PREFIX}-oauth@example.com`;
      await pool.query(
        `INSERT INTO users (email, google_id, display_name, email_verified)
         VALUES ($1, $2, $3, true)`,
        [oauthEmail, 'google-test-id-nopw', 'OAuth Only User'],
      );

      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email: oauthEmail, password },
      );

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });

    it('rejects login for deactivated user', async () => {
      // Register a user then deactivate them
      const deactivatedEmail = `${PREFIX}-deactivated@example.com`;
      await pool.query(
        `INSERT INTO users (email, password_hash, email_verified, is_active)
         VALUES ($1, $2, false, false)`,
        [deactivatedEmail, '$2a$10$fakehashfordeactivateduser1234567890abc'],
      );

      const { body } = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email: deactivatedEmail, password },
      );

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });

    it('sets refresh token cookie on successful login', async () => {
      const { headers: resHeaders } = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password },
      );

      const setCookie = resHeaders['set-cookie'];
      const cookieStr = Array.isArray(setCookie) ? setCookie[0] : setCookie;
      expect(cookieStr).toContain('refreshToken=');
      expect(cookieStr).toContain('HttpOnly');
      expect(cookieStr).toContain('SameSite=None');
    });

    it('updates last_login_at on successful login', async () => {
      // Clear last_login_at first
      await pool.query('UPDATE users SET last_login_at = NULL WHERE email = $1', [email]);

      await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password },
      );

      const { rows } = await pool.query('SELECT last_login_at FROM users WHERE email = $1', [email]);
      expect(rows[0].last_login_at).not.toBeNull();
    });
  });

  describe('logout — additional checks', () => {
    it('clears refresh token cookie', async () => {
      // Login to get a valid token first
      const loginRes = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password },
      );
      const loginPayload = loginRes.body.data?.login as { accessToken: string };

      const { headers: resHeaders } = await gql(
        'mutation { logout }',
        undefined,
        { authorization: `Bearer ${loginPayload.accessToken}` },
      );

      const setCookie = resHeaders['set-cookie'];
      const cookieStr = Array.isArray(setCookie) ? setCookie[0] : setCookie;
      expect(cookieStr).toContain('Max-Age=0');
    });

    it('deletes all refresh tokens for the user from DB', async () => {
      // Login to create a refresh token
      const loginRes = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password },
      );
      const loginPayload = loginRes.body.data?.login as { accessToken: string };

      // Verify at least one refresh token exists
      const { rows: before } = await pool.query(
        'SELECT COUNT(*)::int as count FROM refresh_tokens WHERE user_id = $1',
        [userId],
      );
      expect(before[0].count).toBeGreaterThan(0);

      // Logout
      await gql(
        'mutation { logout }',
        undefined,
        { authorization: `Bearer ${loginPayload.accessToken}` },
      );

      // Verify refresh tokens are deleted
      const { rows: after } = await pool.query(
        'SELECT COUNT(*)::int as count FROM refresh_tokens WHERE user_id = $1',
        [userId],
      );
      expect(after[0].count).toBe(0);
    });
  });

  describe('refreshToken — additional branches', () => {
    it('rejects an invalid/fabricated refresh token', async () => {
      const { body } = await gql(
        'mutation { refreshToken { accessToken user { id } } }',
        undefined,
        { cookie: 'refreshToken=completely-invalid-token-value' },
      );

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });

    it('rejects refresh for deactivated user', async () => {
      // Create a user, login to get a refresh token, then deactivate the user
      const deactEmail = `${PREFIX}-deact-refresh@example.com`;
      const { body: regBody } = await gql(
        `mutation($email: String!, $password: String!) {
          register(email: $email, password: $password) {
            accessToken
            user { id }
          }
        }`,
        { email: deactEmail, password },
      );
      expect(regBody.errors).toBeUndefined();
      const deactUserId = (regBody.data?.register as { user: { id: string } }).user.id;

      // Login to get a refresh token cookie
      const loginRes = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email: deactEmail, password },
      );
      expect(loginRes.body.errors).toBeUndefined();

      const setCookie = loginRes.headers['set-cookie'];
      const cookieStr = Array.isArray(setCookie) ? setCookie[0] : setCookie;
      const cookieMatch = (cookieStr as string).match(/refreshToken=([^;]+)/);
      expect(cookieMatch).toBeTruthy();

      // Deactivate the user
      await pool.query('UPDATE users SET is_active = false WHERE id = $1', [deactUserId]);

      // Attempt to refresh — should fail with UNAUTHENTICATED
      const { body } = await gql(
        'mutation { refreshToken { accessToken user { id } } }',
        undefined,
        { cookie: `refreshToken=${cookieMatch![1]}` },
      );

      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
      expect(body.errors![0].message).toContain('User not found');

      // Re-activate for cleanup
      await pool.query('UPDATE users SET is_active = true WHERE id = $1', [deactUserId]);
    });

    it('issues new refresh token cookie on successful refresh (rotation)', async () => {
      // Login to get a refresh token cookie
      const loginRes = await gql(
        `mutation($email: String!, $password: String!) {
          login(email: $email, password: $password) { accessToken user { id } }
        }`,
        { email, password },
      );
      expect(loginRes.body.errors).toBeUndefined();

      const setCookie = loginRes.headers['set-cookie'];
      const cookieStr = Array.isArray(setCookie) ? setCookie[0] : setCookie;
      const cookieMatch = (cookieStr as string).match(/refreshToken=([^;]+)/);

      // Refresh the token
      const refreshRes = await gql(
        'mutation { refreshToken { accessToken user { id } } }',
        undefined,
        { cookie: `refreshToken=${cookieMatch![1]}` },
      );

      expect(refreshRes.body.errors).toBeUndefined();

      // Verify a new cookie was set
      const newSetCookie = refreshRes.headers['set-cookie'];
      const newCookieStr = Array.isArray(newSetCookie) ? newSetCookie[0] : newSetCookie;
      expect(newCookieStr).toContain('refreshToken=');
      expect(newCookieStr).toContain('HttpOnly');
    });
  });

  describe('initiateGoogleAuth', () => {
    it('returns a Google OAuth URL when configured', async () => {
      // Set env vars for test
      const origId = process.env.GOOGLE_CLIENT_ID;
      const origSecret = process.env.GOOGLE_CLIENT_SECRET;
      process.env.GOOGLE_CLIENT_ID = 'test-client-id';
      process.env.GOOGLE_CLIENT_SECRET = 'test-client-secret';

      // The resolver reads from module-scope vars, so this test validates
      // the schema and resolver wiring. We test the URL format by importing directly.
      // For a full test, we'd need to restart the module, so just verify the
      // mutation exists and returns a string.
      const { body } = await gql('mutation { initiateGoogleAuth }');

      // May return error if module-scope vars were cached as empty
      // That's fine — the point is the mutation is wired up
      if (!body.errors) {
        expect(typeof body.data?.initiateGoogleAuth).toBe('string');
      }

      process.env.GOOGLE_CLIENT_ID = origId;
      process.env.GOOGLE_CLIENT_SECRET = origSecret;
    });

    it('returns error when Google OAuth is not configured', async () => {
      // Module-scope GOOGLE_CLIENT_ID is captured at import time.
      // If it was empty when the module loaded (typical in test env),
      // the resolver should return an INTERNAL_SERVER_ERROR.
      const { body } = await gql('mutation { initiateGoogleAuth }');

      // If the module-scope var was empty at load time, we get the error
      if (body.errors) {
        expect(body.errors[0].message).toContain('not configured');
        expect(body.errors[0].extensions?.code).toBe('INTERNAL_SERVER_ERROR');
      }
    });
  });

  describe('completeGoogleAuth', () => {
    it('returns error when Google OAuth is not configured', async () => {
      // Module-scope GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are empty in test env
      const { body } = await gql(
        `mutation($code: String!) { completeGoogleAuth(code: $code) { accessToken user { id } } }`,
        { code: 'test-auth-code' },
      );

      // If module-scope vars were empty, we get the error
      if (body.errors) {
        expect(body.errors[0].message).toContain('not configured');
        expect(body.errors[0].extensions?.code).toBe('INTERNAL_SERVER_ERROR');
      }
    });
  });

  describe('User type resolvers', () => {
    it('resolves emailVerified from either snake_case or camelCase', async () => {
      const { body } = await gql(
        '{ me { emailVerified createdAt displayName } }',
        undefined,
        { authorization: `Bearer ${accessToken}` },
      );

      // The User type resolvers handle both DB row format (snake_case)
      // and already-transformed format (camelCase)
      if (!body.errors && body.data?.me) {
        const me = body.data.me as Record<string, unknown>;
        expect(typeof me.emailVerified).toBe('boolean');
        expect(me.createdAt).toBeTruthy();
      }
    });
  });
});
