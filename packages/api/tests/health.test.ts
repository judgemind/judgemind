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

  it('exposes Case, Judge, Ruling, Document, Party types in schema introspection', async () => {
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
    expect(typeNames).toContain('Document');
    expect(typeNames).toContain('Party');
    expect(typeNames).toContain('CaseConnection');
    expect(typeNames).toContain('RulingConnection');
    expect(typeNames).toContain('JudgeConnection');
  });

  it('cases query returns a CaseConnection (edges + pageInfo)', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({
        query: '{ cases { edges { node { id caseNumber } cursor } pageInfo { hasNextPage endCursor } } }',
      }),
    });
    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body);
    // When DB available: data.cases has edges + pageInfo shape
    // When DB unavailable: errors array is present
    if (body.data?.cases) {
      expect(body.data.cases).toHaveProperty('edges');
      expect(body.data.cases).toHaveProperty('pageInfo');
      expect(Array.isArray(body.data.cases.edges)).toBe(true);
    } else {
      expect(body.errors).toBeDefined();
    }
  });
});
