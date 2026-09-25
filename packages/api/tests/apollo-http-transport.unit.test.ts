/**
 * HTTP transport contract for the Fastify ↔ Apollo Server bridge in
 * `src/app.ts` (#4694 — Apollo Server 4 → 5 upgrade).
 *
 * `buildApp()` wires Apollo in through `executeHTTPGraphQLRequest` rather than
 * an official integration package, so these tests pin the observable HTTP
 * behavior that a major Apollo bump could silently change:
 *
 *   - POST JSON queries execute and return 200.
 *   - GET queries are served when they carry a CSRF-preflight header.
 *   - CSRF prevention rejects "simple" GET requests (no preflight header, no
 *     non-simple content-type) with 400 — this is the protection
 *     GHSA-9q82-xgwf-vj6h hardens in @apollo/server 5.5.
 *   - Variable-coercion errors return 400 (Apollo Server 5 default for
 *     `status400ForVariableCoercionErrors`; v4 returned 200).
 *
 * None of these queries touch the database: `__typename` resolves without a
 * resolver, CSRF rejection and variable coercion both happen before
 * execution, and requests carry no `Authorization` header so `extractUser`
 * never queries the pool.
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';

let app: FastifyInstance;

beforeAll(async () => {
  app = await buildApp();
});

afterAll(async () => {
  await app.close();
});

describe('Apollo HTTP transport (Fastify bridge)', () => {
  it('executes a POST JSON query and returns 200', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: '{ __typename }' }),
    });
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toMatch(/application\/(graphql-response\+)?json/);
    expect(JSON.parse(res.body)).toEqual({ data: { __typename: 'Query' } });
  });

  it('serves a GET query that carries the apollo-require-preflight header', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/graphql?query=%7B__typename%7D',
      headers: { 'apollo-require-preflight': 'true' },
    });
    expect(res.statusCode).toBe(200);
    expect(JSON.parse(res.body)).toEqual({ data: { __typename: 'Query' } });
  });

  it('rejects a simple GET with no preflight header via CSRF prevention', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/graphql?query=%7B__typename%7D',
    });
    expect(res.statusCode).toBe(400);
    expect(res.body).toMatch(/CSRF/i);
    // The query must not have executed.
    expect(res.body).not.toContain('"__typename"');
  });

  it('rejects a POST with a simple (text/plain) content-type via CSRF prevention', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'text/plain' },
      payload: JSON.stringify({ query: '{ __typename }' }),
    });
    expect(res.statusCode).toBe(400);
    expect(res.body).not.toContain('"__typename"');
  });

  it('returns 400 for variable-coercion errors (Apollo Server 5 default)', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({
        query: 'query ($id: ID!) { ruling(id: $id) { id } }',
        variables: { id: { not: 'an id' } },
      }),
    });
    expect(res.statusCode).toBe(400);
    const body = JSON.parse(res.body);
    expect(body.errors?.[0]?.extensions?.code).toBe('BAD_USER_INPUT');
  });
});
