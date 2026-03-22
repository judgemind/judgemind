import { describe, it, expect, afterAll, vi, beforeEach } from 'vitest';
import type { FastifyInstance } from 'fastify';
import Fastify from 'fastify';
import type { Pool } from 'pg';
import { S3Client } from '@aws-sdk/client-s3';
import { Readable } from 'stream';
import { registerDocumentContent, detectCharset, transcodeToUtf8 } from '../src/rest/document-content';

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

/** Create a mock S3 response body from a string or Buffer. */
function mockS3Response(content: string | Buffer, contentLength?: number): {
  Body: Readable;
  ContentLength: number;
} {
  const buf = typeof content === 'string' ? Buffer.from(content, 'utf-8') : content;
  const stream = Readable.from([buf]);
  return {
    Body: stream,
    ContentLength: contentLength ?? buf.length,
  };
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

const HTML_DOC = {
  id: '00000000-0000-0000-0000-000000000001',
  s3_key: 'ca/la/lasc/raw/2026-01-01/ruling.html',
  s3_bucket: 'judgemind-document-archive-dev',
  format: 'html',
  status: 'active',
};

const PDF_DOC = {
  ...HTML_DOC,
  format: 'pdf',
  s3_key: 'ca/la/lasc/raw/2026-01-01/ruling.pdf',
};

// ---------------------------------------------------------------------------
// Unit tests for helper functions
// ---------------------------------------------------------------------------

describe('detectCharset', () => {
  it('detects charset from <meta charset="...">', () => {
    const html = '<html><head><meta charset="windows-1252"></head><body>test</body></html>';
    expect(detectCharset(html)).toBe('windows-1252');
  });

  it('detects charset from <meta http-equiv="Content-Type">', () => {
    const html =
      '<html><head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1"></head></html>';
    expect(detectCharset(html)).toBe('iso-8859-1');
  });

  it('returns null when no charset is specified', () => {
    const html = '<html><head><title>Test</title></head><body>hello</body></html>';
    expect(detectCharset(html)).toBeNull();
  });

  it('handles utf-8 charset', () => {
    const html = '<meta charset="UTF-8"><p>test</p>';
    expect(detectCharset(html)).toBe('utf-8');
  });
});

describe('transcodeToUtf8', () => {
  it('transcodes windows-1252 to UTF-8', () => {
    // Windows-1252: 0x93 = left double quote, 0x94 = right double quote
    const buf = Buffer.from([0x93, 0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x94]);
    const result = transcodeToUtf8(buf, 'windows-1252');
    expect(result).toBe('\u201cHello\u201d');
  });

  it('falls back to latin1 for unsupported charsets', () => {
    // ASCII-compatible bytes decode the same in latin1 and UTF-8
    const buf = Buffer.from('Hello', 'utf-8');
    const result = transcodeToUtf8(buf, 'totally-fake-charset-12345');
    expect(result).toBe('Hello');
  });

  it('handles UTF-8 content passed through as-is', () => {
    const buf = Buffer.from('Hello \u201cWorld\u201d', 'utf-8');
    const result = transcodeToUtf8(buf, 'utf-8');
    expect(result).toBe('Hello \u201cWorld\u201d');
  });

  it('handles Windows-1252 documents larger than 65KB without throwing', () => {
    // V8 limits spread arguments to ~65,536. A buffer larger than that would
    // crash the old implementation with RangeError: Maximum call stack size exceeded.
    const size = 100_000; // 100 KB — well above the V8 spread limit
    const buf = Buffer.alloc(size);
    // Fill with ASCII 'A' (0x41) and sprinkle some Windows-1252 special chars
    buf.fill(0x41);
    buf[0] = 0x93;   // left double quote -> U+201C
    buf[size - 1] = 0x94; // right double quote -> U+201D

    const result = transcodeToUtf8(buf, 'windows-1252');

    expect(result.length).toBe(size);
    expect(result[0]).toBe('\u201c');
    expect(result[size - 1]).toBe('\u201d');
    // Middle chars should all be 'A'
    expect(result[1]).toBe('A');
    expect(result[size - 2]).toBe('A');
  });
});

// ---------------------------------------------------------------------------
// Integration tests for the endpoint
// ---------------------------------------------------------------------------

describe('GET /api/documents/:id/content', () => {
  let app: FastifyInstance;
  let mockPool: Pool;

  beforeEach(() => {
    vi.clearAllMocks();
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
    registerDocumentContent(app, mockPool, s3Client ?? createMockS3Client());
    return app;
  }

  it('returns 400 for invalid UUID', async () => {
    const server = await buildTestApp();
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/not-a-uuid/content',
    });
    expect(res.statusCode).toBe(400);
    expect(JSON.parse(res.body)).toEqual({ error: 'Invalid document ID format' });
  });

  it('returns 404 when document does not exist', async () => {
    const server = await buildTestApp([]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(404);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document not found' });
  });

  it('returns 410 for non-active documents', async () => {
    const server = await buildTestApp([{ ...HTML_DOC, status: 'superseded' }]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(410);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document is no longer available' });
  });

  it('returns 400 for non-HTML documents', async () => {
    const server = await buildTestApp([PDF_DOC]);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(400);
    expect(JSON.parse(res.body)).toEqual({
      error: 'Content endpoint only supports HTML documents. Use /download for other formats.',
    });
  });

  it('returns HTML content with correct content-type header', async () => {
    const htmlContent = '<html><body><p>The motion is GRANTED.</p></body></html>';
    const s3Client = createMockS3Client(() => Promise.resolve(mockS3Response(htmlContent)));

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toBe('text/html; charset=utf-8');
    expect(res.body).toBe(htmlContent);
  });

  it('sets cache-control header', async () => {
    const htmlContent = '<p>Content</p>';
    const s3Client = createMockS3Client(() => Promise.resolve(mockS3Response(htmlContent)));

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.headers['cache-control']).toBe('public, max-age=3600');
  });

  it('transcodes windows-1252 content to UTF-8', async () => {
    // Build HTML with a meta charset tag and windows-1252 encoded smart quotes
    const metaTag = '<html><head><meta charset="windows-1252"></head><body>';
    const metaBuf = Buffer.from(metaTag, 'utf-8');
    // 0x93 = left double quote in windows-1252, 0x94 = right double quote
    const bodyBuf = Buffer.from([0x93, 0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x94]);
    const closeBuf = Buffer.from('</body></html>', 'utf-8');
    const fullBuf = Buffer.concat([metaBuf, bodyBuf, closeBuf]);

    const s3Client = createMockS3Client(() => Promise.resolve(mockS3Response(fullBuf)));

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(200);
    // The smart quotes should be properly transcoded
    expect(res.body).toContain('\u201c');
    expect(res.body).toContain('\u201d');
    expect(res.body).toContain('Hello');
  });

  it('returns 404 when S3 object does not exist', async () => {
    const noSuchKeyError = new Error('The specified key does not exist.');
    noSuchKeyError.name = 'NoSuchKey';
    const s3Client = createMockS3Client(() => Promise.reject(noSuchKeyError));

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(404);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document file not found in storage' });
  });

  it('returns 500 when S3 fails with infrastructure error', async () => {
    const infraError = new Error('Network timeout');
    infraError.name = 'TimeoutError';
    const s3Client = createMockS3Client(() => Promise.reject(infraError));

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(500);
    expect(JSON.parse(res.body)).toEqual({ error: 'Failed to fetch document content' });
  });

  it('returns 413 for documents exceeding max size', async () => {
    const s3Client = createMockS3Client(() =>
      Promise.resolve({
        Body: Readable.from([Buffer.from('small')]),
        ContentLength: 10 * 1024 * 1024, // 10 MB
      }),
    );

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(413);
    expect(JSON.parse(res.body)).toEqual({
      error: 'Document is too large for inline viewing. Use /download instead.',
    });
  });

  it('returns 413 when streaming body exceeds max size without ContentLength header', async () => {
    // Simulate an S3 response with no ContentLength header whose body exceeds 5 MB.
    // We use multiple chunks that together exceed MAX_CONTENT_SIZE (5 * 1024 * 1024).
    const chunkSize = 1024 * 1024; // 1 MB per chunk
    const chunkCount = 6; // 6 MB total — exceeds the 5 MB limit
    const chunks: Buffer[] = [];
    for (let i = 0; i < chunkCount; i++) {
      chunks.push(Buffer.alloc(chunkSize, 0x41)); // Fill with 'A'
    }

    const s3Client = createMockS3Client(() =>
      Promise.resolve({
        Body: Readable.from(chunks),
        // No ContentLength — simulates chunked transfer encoding
      }),
    );

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(413);
    expect(JSON.parse(res.body)).toEqual({
      error: 'Document is too large for inline viewing. Use /download instead.',
    });
  });

  it('returns 404 when S3 response body is empty', async () => {
    const s3Client = createMockS3Client(() =>
      Promise.resolve({ Body: null, ContentLength: 0 }),
    );

    const server = await buildTestApp([HTML_DOC], s3Client);
    const res = await server.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-0000-0000-000000000001/content',
    });
    expect(res.statusCode).toBe(404);
    expect(JSON.parse(res.body)).toEqual({ error: 'Document content is empty' });
  });
});
