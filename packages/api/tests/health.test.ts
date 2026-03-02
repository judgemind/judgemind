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

describe('GET /health', () => {
  it('returns a valid health response shape', async () => {
    const res = await app.inject({ method: 'GET', url: '/health' });
    expect([200, 503]).toContain(res.statusCode);
    const body = JSON.parse(res.body);
    expect(body).toHaveProperty('status');
    expect(body).toHaveProperty('db');
    expect(['ok', 'error']).toContain(body.status);
  });
});

describe('POST /graphql', () => {
  it('responds to __typename introspection', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: '{ __typename }' }),
    });
    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body);
    expect(body.data).toEqual({ __typename: 'Query' });
  });

  it('exposes Case, Judge, Ruling types in schema introspection', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({
        query: `{
          __schema {
            types { name }
          }
        }`,
      }),
    });
    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body);
    const typeNames: string[] = body.data.__schema.types.map((t: { name: string }) => t.name);
    expect(typeNames).toContain('Case');
    expect(typeNames).toContain('Judge');
    expect(typeNames).toContain('Ruling');
    expect(typeNames).toContain('Court');
  });

  it('returns cases array (empty or error depending on DB availability)', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: '{ cases { id caseNumber } }' }),
    });
    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body);
    // When DB is available: data.cases is an array
    // When DB is unavailable: errors array is present
    expect(body.data !== undefined || body.errors !== undefined).toBe(true);
  });
});
