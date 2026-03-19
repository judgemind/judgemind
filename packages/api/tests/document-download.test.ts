import { describe, it, expect, afterAll, vi, beforeEach } from 'vitest';
import type { FastifyInstance } from 'fastify';
import Fastify from 'fastify';
import type { Pool } from 'pg';
import { S3Client } from '@aws-sdk/client-s3';
import { registerDocumentDownload } from '../src/rest/document-download';

// ---------------------------------------------------------------------------
// Mock S3 — we don't want real AWS calls in unit tests
// ---------------------------------------------------------------------------

const mockGetSignedUrl = vi.fn().mockResolvedValue('https://s3.example.com/presigned-url');

vi.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: (...args: unknown[]) => mockGetSignedUrl(...args),
}));

// ---------------------------------------------------------------------------
// Mock S3 client with controllable send()
// ---------------------------------------------------------------------------

function createMockS3Client(sendImpl?: (...args: unknown[]) => Promise<unknown>): S3Client {
  const client = {
    send: vi.fn().mockResolvedValue({}),
  } as unknown as S3Client;
  if (sendImpl) {
    (client.send as ReturnType<typeof vi.fn>).mockImplementation(sendImpl);
  }
  return client;
}

// ---------------------------------------------------------------------------
// Mock pool
// ---------------------------------------------------------------------------

function createMockPool(rows: Record<string, unknown>[] = []): Pool {
  return {
    query: vi.fn().mockResolvedValue({ rows }),
  } as unknown as Pool;
}

// ---------------------------------------------------------------------------
// Shared test data
// ---------------------------------------------------------------------------

const ACTIVE_DOC = {
  id: '00000000-0000-0000-0000-000000000001',
  s3_key: 'ca/la/lasc/raw/2026-01-01/doc.pdf',
  s3_bucket: 'judgemind-document-archive-dev',
  format: 'pdf',
  status: 'active',
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('GET /api/documents/:id/download', () => {
  let app: FastifyInstance;
  let mockPool: Pool;

  beforeEach(() => {
    mockGetSignedUrl.mockClear();
    mockGetSignedUrl.mockResolvedValue('https://s3.example.com/presigned-url');
  });

  afterAll(async () => {
    if (app) await app.close();
  });

  async function buildTestApp(
    rows: Record<string, unknown>[] = [],
    s3Client?: S3Client,
  ): Promise<FastifyInstance> {
    if (app) await app.close();
    app = Fastify({ logger: false });
    mockPool = createMockPool(rows);
    registerDocumentDownload(app, mockPool, s3Client ?? createMockS3Client());
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
      { ...ACTIVE_DOC, status: 'superseded' },
    ]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(410);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document is no longer available' });
  });

  it('redirects to presigned URL for active document', async () => {
    const server = await buildTestApp([ACTIVE_DOC]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(302);
    expect(res.headers.location).toBe('https://s3.example.com/presigned-url');
  });

  it('queries the database with the correct document ID', async () => {
    const docId = '11111111-2222-3333-4444-555555555555';
    const server = await buildTestApp([
      { ...ACTIVE_DOC, id: docId, s3_key: 'test/key', s3_bucket: 'test-bucket', format: 'html' },
    ]);
    await server.inject({
      method: 'GET',
      url: `/api/documents/${docId}/download`,
    });
    expect(mockPool.query).toHaveBeenCalledWith(
      'SELECT id, s3_key, s3_bucket, format, status FROM documents WHERE id = $1',
      [docId],
    );
  });

  it('returns 404 when S3 object does not exist (NoSuchKey)', async () => {
    const noSuchKeyError = new Error('The specified key does not exist.');
    noSuchKeyError.name = 'NoSuchKey';
    const s3Client = createMockS3Client(() => Promise.reject(noSuchKeyError));

    const server = await buildTestApp([ACTIVE_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(404);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document file not found in storage' });
  });

  it('returns 404 when S3 object does not exist (NotFound)', async () => {
    const notFoundError = new Error('Not Found');
    notFoundError.name = 'NotFound';
    const s3Client = createMockS3Client(() => Promise.reject(notFoundError));

    const server = await buildTestApp([ACTIVE_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(404);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document file not found in storage' });
  });

  it('returns 500 when S3 presigned URL generation fails', async () => {
    const s3Client = createMockS3Client();
    mockGetSignedUrl.mockRejectedValueOnce(new Error('Credential resolution failed'));

    const server = await buildTestApp([ACTIVE_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(500);
    expect(JSON.parse(res.body)).toEqual({ error: 'Failed to generate download URL' });
  });

  it('returns 500 when HeadObject fails with infrastructure error', async () => {
    const infraError = new Error('Network timeout');
    infraError.name = 'TimeoutError';
    const s3Client = createMockS3Client(() => Promise.reject(infraError));

    const server = await buildTestApp([ACTIVE_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/download',
    });
    expect(res.statusCode).toBe(500);
    expect(JSON.parse(res.body)).toEqual({ error: 'Failed to generate download URL' });
  });
});
