import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import type { FastifyInstance } from 'fastify';

// Mock the pg pool so tests don't require a real database.
vi.mock('../../src/data-access/pool.js', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [] }),
    end: vi.fn().mockResolvedValue(undefined),
  },
}));

// Import after mocks are registered.
const { buildApp } = await import('../../src/app.js');

describe('API server', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildApp();
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
  });

  it('GET /health returns 200 with status ok', async () => {
    const res = await app.inject({ method: 'GET', url: '/health' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ status: 'ok', db: 'ok' });
  });

  it('POST /graphql responds to introspection', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: '{ __typename }' }),
    });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body).toHaveProperty('data.__typename', 'Query');
  });

  it('POST /graphql returns errors for invalid query', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: '{ notAField }' }),
    });
    // Apollo Server 4 returns 400 for schema validation errors
    expect(res.statusCode).toBe(400);
    const body = res.json();
    expect(body).toHaveProperty('errors');
  });
});
