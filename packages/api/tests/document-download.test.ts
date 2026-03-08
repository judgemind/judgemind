import { describe, it, expect, afterAll, vi } from 'vitest';
import type { FastifyInstance } from 'fastify';
import Fastify from 'fastify';
import type { Pool } from 'pg';
import { registerDocumentDownload } from '../src/rest/document-download';

// ---------------------------------------------------------------------------
// Mock S3 — we don't want real AWS calls in unit tests
// ---------------------------------------------------------------------------

vi.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: vi.fn().mockResolvedValue('https://s3.example.com/presigned-url'),
}));

// ---------------------------------------------------------------------------
// Mock pool
// ---------------------------------------------------------------------------

function createMockPool(rows: Record<string, unknown>[] = []): Pool {
  return {
    query: vi.fn().mockResolvedValue({ rows }),
  } as unknown as Pool;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('GET /api/documents/:id/download', () => {
  let app: FastifyInstance;
  let mockPool: Pool;

  afterAll(async () => {
    if (app) await app.close();
  });

  async function buildTestApp(rows: Record<string, unknown>[] = []): Promise<FastifyInstance> {
    if (app) await app.close();
    app = Fastify({ logger: false });
    mockPool = createMockPool(rows);
    registerDocumentDownload(app, mockPool);
    return app;
  }

  it('returns 400 for invalid UUID', async () => {
    const server = await buildTestApp();
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/not-a-uuid/download',
    });
    expect(res.statusCode).toBe(400);
    expect(JSON.parse(res.body)).toEqual({ error: 'Invalid document ID format' });
  });

  it('returns 404 when document does not exist', async () => {
    const server = await buildTestApp([]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(404);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document not found' });
  });

  it('returns 410 for non-active documents', async () => {
    const server = await buildTestApp([
      {
        id: '00000000-0000-0000-0000-000000000001',
        s3_key: 'ca/la/lasc/raw/2026-01-01/doc.pdf',
        s3_bucket: 'judgemind-document-archive-dev',
        format: 'pdf',
        status: 'superseded',
      },
    ]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(410);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document is no longer available' });
  });

  it('redirects to presigned URL for active document', async () => {
    const server = await buildTestApp([
      {
        id: '00000000-0000-0000-0000-000000000001',
        s3_key: 'ca/la/lasc/raw/2026-01-01/doc.pdf',
        s3_bucket: 'judgemind-document-archive-dev',
        format: 'pdf',
        status: 'active',
      },
    ]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(302);
    expect(res.headers.location).toBe('https://s3.example.com/presigned-url');
  });

  it('queries the database with the correct document ID', async () => {
    const server = await buildTestApp([
      {
        id: '11111111-2222-3333-4444-555555555555',
        s3_key: 'test/key',
        s3_bucket: 'test-bucket',
        format: 'html',
        status: 'active',
      },
    ]);
    await server.inject({
      method: 'GET',
      url: '/api/documents/11111111-2222-3333-4444-555555555555/download',
    });
    expect(mockPool.query).toHaveBeenCalledWith(
      'SELECT id, s3_key, s3_bucket, format, status FROM documents WHERE id = $1',
      ['11111111-2222-3333-4444-555555555555'],
    );
  });
});
