/**
 * Integration tests for the REST API v1 endpoints.
 *
 * Uses Fastify's inject() to fire requests without a real HTTP server.
 * The database pool is mocked to return canned rows, avoiding a real DB.
 */

import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';
import type { Pool } from 'pg';

// ---------------------------------------------------------------------------
// Mock DB pool
// ---------------------------------------------------------------------------

const UUID_A = '00000000-0000-0000-0000-000000000001';
const UUID_B = '00000000-0000-0000-0000-000000000002';
const TS = '2024-01-15T10:00:00.000Z';

const caseRow = {
  id: UUID_A,
  case_number: 'CV-2024-001',
  case_title: 'Doe v. Smith',
  case_type: 'civil',
  case_status: 'active',
  court_id: UUID_B,
  filed_at: '2024-01-01',
  created_at: TS,
};

const judgeRow = {
  id: UUID_A,
  canonical_name: 'Jane Smith',
  court_id: UUID_B,
  department: '12',
  is_active: true,
  appointed_at: '2020-01-01',
};

const attorneyRow = {
  id: UUID_A,
  canonical_name: 'John Doe',
  bar_number: '12345',
  bar_state: 'CA',
  firm_name: 'Doe LLP',
  is_active: true,
};

const documentRow = {
  id: UUID_A,
  case_id: UUID_B,
  court_id: UUID_B,
  document_type: 'ruling',
  motion_type: 'msj',
  s3_key: 'rulings/2024/doc.html',
  s3_bucket: 'judgemind-document-archive-dev',
  format: 'html',
  content_hash: 'abc123',
  source_url: null,
  scraper_id: 'la-superior',
  captured_at: TS,
  hearing_date: '2024-01-15',
  status: 'active',
};

const rulingRow = {
  id: UUID_A,
  document_id: UUID_B,
  case_id: UUID_B,
  judge_id: UUID_B,
  court_id: UUID_B,
  outcome: 'granted',
  motion_type: 'msj',
  hearing_date: '2024-01-15',
  posted_at: TS,
  department: '12',
  is_tentative: true,
  ruling_number: '1',
  summary: null,
  ruling_text: 'The motion is granted.',
};

const apiUserRow = {
  id: UUID_B,
  email: 'test@example.com',
  role: 'user',
};

function makePool(rows: Record<string, unknown>[]): Pool {
  return {
    query: vi.fn().mockImplementation(async (sql: string, params: unknown[]) => {
      // API key lookup for authentication
      if (typeof sql === 'string' && sql.includes('api_key')) {
        const key = params[0];
        return { rows: key === 'valid-api-key' ? [apiUserRow] : [] };
      }
      return { rows };
    }),
    end: vi.fn().mockResolvedValue(undefined),
  } as unknown as Pool;
}

// ---------------------------------------------------------------------------
// Test setup
// ---------------------------------------------------------------------------

let app: FastifyInstance;

beforeAll(async () => {
  // Build the app with a pool that returns rulingRow for all data queries.
  // Each test group overrides this as needed via its own app instance.
  app = await buildApp(makePool([caseRow]), undefined);
});

afterAll(async () => {
  await app.close();
});

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function inject(
  instance: FastifyInstance,
  method: string,
  url: string,
  headers?: Record<string, string>,
) {
  return instance.inject({ method, url, headers });
}

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

describe('GET /v1/cases', () => {
  it('returns 200 with data array and pagination', async () => {
    const res = await inject(app, 'GET', '/v1/cases');
    expect(res.statusCode).toBe(200);
    const body = res.json<{ data: unknown[]; pagination: { has_more: boolean; next_cursor: string | null } }>();
    expect(Array.isArray(body.data)).toBe(true);
    expect(body.data[0]).toMatchObject({ id: UUID_A, case_number: 'CV-2024-001' });
    expect(typeof body.pagination.has_more).toBe('boolean');
  });

  it('accepts filter query params without error', async () => {
    const res = await inject(app, 'GET', '/v1/cases?case_status=active&case_type=civil&limit=5');
    expect(res.statusCode).toBe(200);
  });
});

describe('GET /v1/cases/:id', () => {
  it('returns 200 with data object when found', async () => {
    const res = await inject(app, 'GET', `/v1/cases/${UUID_A}`);
    expect(res.statusCode).toBe(200);
    const body = res.json<{ data: { id: string } }>();
    expect(body.data.id).toBe(UUID_A);
  });

  it('returns 404 when not found', async () => {
    const emptyApp = await buildApp(makePool([]), undefined);
    try {
      const res = await inject(emptyApp, 'GET', `/v1/cases/${UUID_A}`);
      expect(res.statusCode).toBe(404);
      expect(res.json<{ error: string }>().error).toBe('Not Found');
    } finally {
      await emptyApp.close();
    }
  });
});

// ---------------------------------------------------------------------------
// Judges
// ---------------------------------------------------------------------------

describe('GET /v1/judges', () => {
  let judgeApp: FastifyInstance;

  beforeAll(async () => {
    judgeApp = await buildApp(makePool([judgeRow]), undefined);
  });

  afterAll(async () => {
    await judgeApp.close();
  });

  it('returns 200 with data array', async () => {
    const res = await inject(judgeApp, 'GET', '/v1/judges');
    expect(res.statusCode).toBe(200);
    const body = res.json<{ data: { canonical_name: string }[] }>();
    expect(body.data[0]?.canonical_name).toBe('Jane Smith');
  });

  it('accepts is_active filter', async () => {
    const res = await inject(judgeApp, 'GET', '/v1/judges?is_active=true');
    expect(res.statusCode).toBe(200);
  });
});

describe('GET /v1/judges/:id', () => {
  let judgeApp: FastifyInstance;

  beforeAll(async () => {
    judgeApp = await buildApp(makePool([judgeRow]), undefined);
  });

  afterAll(async () => {
    await judgeApp.close();
  });

  it('returns 200 when found', async () => {
    const res = await inject(judgeApp, 'GET', `/v1/judges/${UUID_A}`);
    expect(res.statusCode).toBe(200);
    expect(res.json<{ data: { id: string } }>().data.id).toBe(UUID_A);
  });

  it('returns 404 when not found', async () => {
    const emptyApp = await buildApp(makePool([]), undefined);
    try {
      const res = await inject(emptyApp, 'GET', `/v1/judges/${UUID_A}`);
      expect(res.statusCode).toBe(404);
    } finally {
      await emptyApp.close();
    }
  });
});

// ---------------------------------------------------------------------------
// Attorneys
// ---------------------------------------------------------------------------

describe('GET /v1/attorneys', () => {
  let attyApp: FastifyInstance;

  beforeAll(async () => {
    attyApp = await buildApp(makePool([attorneyRow]), undefined);
  });

  afterAll(async () => {
    await attyApp.close();
  });

  it('returns 200 with data array', async () => {
    const res = await inject(attyApp, 'GET', '/v1/attorneys');
    expect(res.statusCode).toBe(200);
    const body = res.json<{ data: { canonical_name: string }[] }>();
    expect(body.data[0]?.canonical_name).toBe('John Doe');
  });

  it('accepts bar_state filter', async () => {
    const res = await inject(attyApp, 'GET', '/v1/attorneys?bar_state=CA');
    expect(res.statusCode).toBe(200);
  });
});

describe('GET /v1/attorneys/:id', () => {
  let attyApp: FastifyInstance;

  beforeAll(async () => {
    attyApp = await buildApp(makePool([attorneyRow]), undefined);
  });

  afterAll(async () => {
    await attyApp.close();
  });

  it('returns 200 when found', async () => {
    const res = await inject(attyApp, 'GET', `/v1/attorneys/${UUID_A}`);
    expect(res.statusCode).toBe(200);
    expect(res.json<{ data: { id: string } }>().data.id).toBe(UUID_A);
  });

  it('returns 404 when not found', async () => {
    const emptyApp = await buildApp(makePool([]), undefined);
    try {
      const res = await inject(emptyApp, 'GET', `/v1/attorneys/${UUID_A}`);
      expect(res.statusCode).toBe(404);
    } finally {
      await emptyApp.close();
    }
  });
});

// ---------------------------------------------------------------------------
// Documents
// ---------------------------------------------------------------------------

describe('GET /v1/documents', () => {
  let docApp: FastifyInstance;

  beforeAll(async () => {
    docApp = await buildApp(makePool([documentRow]), undefined);
  });

  afterAll(async () => {
    await docApp.close();
  });

  it('returns 200 with data array', async () => {
    const res = await inject(docApp, 'GET', '/v1/documents');
    expect(res.statusCode).toBe(200);
    const body = res.json<{ data: { id: string }[] }>();
    expect(body.data[0]?.id).toBe(UUID_A);
  });

  it('accepts document_type and status filters', async () => {
    const res = await inject(docApp, 'GET', '/v1/documents?document_type=ruling&status=active');
    expect(res.statusCode).toBe(200);
  });
});

describe('GET /v1/documents/:id', () => {
  let docApp: FastifyInstance;

  beforeAll(async () => {
    docApp = await buildApp(makePool([documentRow]), undefined);
  });

  afterAll(async () => {
    await docApp.close();
  });

  it('returns 200 when found', async () => {
    const res = await inject(docApp, 'GET', `/v1/documents/${UUID_A}`);
    expect(res.statusCode).toBe(200);
    expect(res.json<{ data: { id: string } }>().data.id).toBe(UUID_A);
  });

  it('returns 404 when not found', async () => {
    const emptyApp = await buildApp(makePool([]), undefined);
    try {
      const res = await inject(emptyApp, 'GET', `/v1/documents/${UUID_A}`);
      expect(res.statusCode).toBe(404);
    } finally {
      await emptyApp.close();
    }
  });
});

// ---------------------------------------------------------------------------
// Rulings
// ---------------------------------------------------------------------------

describe('GET /v1/rulings', () => {
  let rulingApp: FastifyInstance;

  beforeAll(async () => {
    rulingApp = await buildApp(makePool([rulingRow]), undefined);
  });

  afterAll(async () => {
    await rulingApp.close();
  });

  it('returns 200 with data array', async () => {
    const res = await inject(rulingApp, 'GET', '/v1/rulings');
    expect(res.statusCode).toBe(200);
    const body = res.json<{ data: { id: string; outcome: string }[] }>();
    expect(body.data[0]?.outcome).toBe('granted');
  });

  it('accepts all filter params', async () => {
    const res = await inject(
      rulingApp,
      'GET',
      `/v1/rulings?judge_id=${UUID_B}&outcome=granted&date_from=2024-01-01&date_to=2024-12-31`,
    );
    expect(res.statusCode).toBe(200);
  });
});

describe('GET /v1/rulings/:id', () => {
  let rulingApp: FastifyInstance;

  beforeAll(async () => {
    rulingApp = await buildApp(makePool([rulingRow]), undefined);
  });

  afterAll(async () => {
    await rulingApp.close();
  });

  it('returns 200 when found', async () => {
    const res = await inject(rulingApp, 'GET', `/v1/rulings/${UUID_A}`);
    expect(res.statusCode).toBe(200);
    expect(res.json<{ data: { id: string } }>().data.id).toBe(UUID_A);
  });

  it('returns 404 when not found', async () => {
    const emptyApp = await buildApp(makePool([]), undefined);
    try {
      const res = await inject(emptyApp, 'GET', `/v1/rulings/${UUID_A}`);
      expect(res.statusCode).toBe(404);
    } finally {
      await emptyApp.close();
    }
  });
});

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

describe('API key authentication', () => {
  let authApp: FastifyInstance;

  beforeAll(async () => {
    authApp = await buildApp(makePool([caseRow]), undefined);
  });

  afterAll(async () => {
    await authApp.close();
  });

  it('accepts requests without API key (unauthenticated)', async () => {
    const res = await inject(authApp, 'GET', '/v1/cases');
    expect(res.statusCode).toBe(200);
  });

  it('accepts requests with valid X-API-Key header', async () => {
    const res = await inject(authApp, 'GET', '/v1/cases', { 'X-API-Key': 'valid-api-key' });
    expect(res.statusCode).toBe(200);
  });

  it('ignores invalid API keys and continues as unauthenticated', async () => {
    // An unknown key returns no rows — request still goes through (no 401)
    const res = await inject(authApp, 'GET', '/v1/cases', { 'X-API-Key': 'bad-key' });
    expect(res.statusCode).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// OpenAPI spec
// ---------------------------------------------------------------------------

describe('OpenAPI spec', () => {
  let specApp: FastifyInstance;

  beforeAll(async () => {
    specApp = await buildApp(makePool([]), undefined);
  });

  afterAll(async () => {
    await specApp.close();
  });

  it('serves the OpenAPI JSON spec at /docs/json', async () => {
    const res = await inject(specApp, 'GET', '/docs/json');
    expect(res.statusCode).toBe(200);
    const spec = res.json<{ openapi: string; paths: Record<string, unknown> }>();
    expect(spec.openapi).toBe('3.0.0');
    expect(spec.paths['/cases']).toBeDefined();
    expect(spec.paths['/judges']).toBeDefined();
    expect(spec.paths['/attorneys']).toBeDefined();
    expect(spec.paths['/documents']).toBeDefined();
    expect(spec.paths['/rulings']).toBeDefined();
  });
});
