/**
 * Pins the zod-backed input validation in auth-resolvers.ts and
 * alert-resolvers.ts across the zod 3 -> 4 upgrade (#4885).
 *
 * zod 4 changed default issue messages and made z.uuid() check RFC 9562
 * version/variant bits. The web register/login pages show the first issue's
 * message verbatim, and alert filters reference ids stored in Postgres uuid
 * columns, so both behaviours are asserted here without a database.
 */

import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import type { FastifyReply } from 'fastify';
import { GraphQLError } from 'graphql';
import { authResolvers } from '../src/graphql/auth-resolvers';
import { alertResolvers } from '../src/graphql/alert-resolvers';
import type { AuthUser } from '../src/auth';

function poolReturning(rows: Array<Record<string, unknown>>): Pool & {
  query: ReturnType<typeof vi.fn>;
} {
  return { query: vi.fn(async () => ({ rows, rowCount: rows.length })) } as unknown as Pool & {
    query: ReturnType<typeof vi.fn>;
  };
}

async function badInput(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (err) {
    expect(err).toBeInstanceOf(GraphQLError);
    expect((err as GraphQLError).extensions.code).toBe('BAD_USER_INPUT');
    return (err as GraphQLError).message;
  }
  throw new Error('expected a BAD_USER_INPUT GraphQLError');
}

describe('register input validation', () => {
  const ctx = () => ({
    pool: poolReturning([{ id: 'existing' }]),
    reply: {} as FastifyReply,
    cookieHeader: '',
  });
  const register = (args: { email: string; password: string; displayName?: string }) =>
    authResolvers.Mutation.register({}, args, ctx() as never);

  it('rejects a malformed email with a readable message', async () => {
    expect(await badInput(register({ email: 'not-an-email', password: 'longenough' }))).toBe(
      'Invalid email address',
    );
  });

  it('rejects an email longer than 255 characters', async () => {
    const email = `${'a'.repeat(250)}@example.com`;
    expect(await badInput(register({ email, password: 'longenough' }))).toBe(
      'Email must be at most 255 characters',
    );
  });

  it('rejects a short password with the same wording the web page uses', async () => {
    expect(await badInput(register({ email: 'a@example.com', password: 'short' }))).toBe(
      'Password must be at least 8 characters',
    );
  });

  it('rejects a password longer than 128 characters', async () => {
    expect(
      await badInput(register({ email: 'a@example.com', password: 'x'.repeat(129) })),
    ).toBe('Password must be at most 128 characters');
  });

  it('rejects a display name longer than 100 characters', async () => {
    expect(
      await badInput(
        register({ email: 'a@example.com', password: 'longenough', displayName: 'd'.repeat(101) }),
      ),
    ).toBe('Display name must be at most 100 characters');
  });

  it('lets valid input through to the database lookup', async () => {
    const c = ctx();
    await expect(
      authResolvers.Mutation.register(
        {},
        { email: 'a@example.com', password: 'longenough' },
        c as never,
      ),
    ).rejects.toThrow('An account with this email already exists');
    expect(c.pool.query).toHaveBeenCalledWith('SELECT id FROM users WHERE email = $1', [
      'a@example.com',
    ]);
  });
});

describe('login input validation', () => {
  it('rejects a malformed email before rate limiting or the database', async () => {
    const pool = poolReturning([]);
    const msg = await badInput(
      authResolvers.Mutation.login(
        {},
        { email: 'nope', password: 'x' },
        { pool, reply: {} as FastifyReply, cookieHeader: '', ip: '127.0.0.1' } as never,
      ),
    );
    expect(msg).toBe('Invalid email address');
    expect(pool.query).not.toHaveBeenCalled();
  });
});

describe('createAlertSubscription filter validation', () => {
  const user = { id: 'user-1', email: 'u@example.com', role: 'user' } as AuthUser;
  const create = (alertType: string, filters: unknown, pool = poolReturning([{ id: 'sub-1' }])) =>
    alertResolvers.Mutation.createAlertSubscription(
      {},
      { alertType, filters: JSON.stringify(filters) },
      { pool, user },
    );

  it('accepts a non-RFC-variant id, matching zod 3 uuid() behaviour', async () => {
    // Version nibble 1, variant nibble 1: zod 4's z.uuid() would reject this.
    const id = '12345678-1234-1234-1234-123456789abc';
    const pool = poolReturning([{ id: 'sub-1' }]);
    await expect(create('judge_ruling', { judge_id: id }, pool)).resolves.toEqual({ id: 'sub-1' });
    expect(pool.query.mock.calls[0][1]).toEqual([
      'user-1',
      'judge_ruling',
      JSON.stringify({ judge_id: id }),
    ]);
  });

  it.each([
    ['judge_ruling', 'judge_id'],
    ['case_docket', 'case_id'],
    ['party_attorney', 'party_id'],
  ])('%s rejects a non-uuid %s', async (alertType, key) => {
    expect(await badInput(create(alertType, { [key]: 'not-a-uuid' }))).toBe('Invalid uuid');
  });

  it('rejects unknown filter keys (strict object)', async () => {
    const msg = await badInput(
      create('judge_ruling', { judge_id: '12345678-1234-1234-1234-123456789abc', evil: 'x' }),
    );
    expect(msg).toContain('evil');
  });

  it('rejects an empty keyword', async () => {
    expect(await badInput(create('keyword', { keyword: '' }))).toBe('keyword must not be empty');
  });

  it('rejects a keyword longer than 200 characters', async () => {
    expect(await badInput(create('keyword', { keyword: 'k'.repeat(201) }))).toBe(
      'keyword must be at most 200 characters',
    );
  });

  it('accepts a valid keyword', async () => {
    await expect(create('keyword', { keyword: 'demurrer' })).resolves.toEqual({ id: 'sub-1' });
  });
});
