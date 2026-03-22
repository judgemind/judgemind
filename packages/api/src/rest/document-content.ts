import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { S3Client, GetObjectCommand, NoSuchKey } from '@aws-sdk/client-s3';
import type { Readable } from 'stream';

/** Max content size to serve inline (5 MB). Documents larger than this should
 *  use the download endpoint instead. */
const MAX_CONTENT_SIZE = 5 * 1024 * 1024;

/**
 * Detect charset from HTML content by inspecting <meta> tags.
 *
 * Checks for:
 * - `<meta charset="windows-1252">`
 * - `<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">`
 *
 * Returns the charset name (lowercased) or null if not found.
 */
export function detectCharset(html: string): string | null {
  // <meta charset="...">
  const metaCharset = /<meta\s[^>]*charset\s*=\s*["']?([^"'\s;>]+)/i.exec(html);
  if (metaCharset) return metaCharset[1].toLowerCase();

  // <meta http-equiv="Content-Type" content="text/html; charset=...">
  const httpEquiv =
    /<meta\s[^>]*http-equiv\s*=\s*["']?content-type["']?\s[^>]*content\s*=\s*["'][^"']*charset\s*=\s*([^"'\s;>]+)/i.exec(
      html,
    );
  if (httpEquiv) return httpEquiv[1].toLowerCase();

  return null;
}

/**
 * Transcode a Buffer from a given charset to UTF-8.
 * Uses the standard TextDecoder API which supports most legacy charsets.
 */
export function transcodeToUtf8(buffer: Buffer, charset: string): string {
  try {
    const decoder = new TextDecoder(charset);
    return decoder.decode(buffer);
  } catch {
    // If the charset is not supported by TextDecoder, fall back to UTF-8
    return buffer.toString('utf-8');
  }
}

/**
 * Register the document content REST endpoint.
 *
 * GET /api/documents/:id/content
 *
 * Fetches the raw HTML content of a document from S3, detects and fixes
 * encoding, and returns it as UTF-8 text/html. Only serves HTML documents;
 * other formats should use the /download endpoint.
 */
export function registerDocumentContent(
  app: FastifyInstance,
  pool: Pool,
  s3Client?: S3Client,
): void {
  const s3 = s3Client ?? new S3Client({
    region: process.env.AWS_REGION ?? 'us-west-2',
  });

  app.get<{ Params: { id: string } }>(
    '/api/documents/:id/content',
    async (req, reply) => {
      const { id } = req.params;

      // Validate UUID format
      const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
      if (!UUID_RE.test(id)) {
        return reply.status(400).send({ error: 'Invalid document ID format' });
      }

      type DocRow = {
        id: string;
        s3_key: string;
        s3_bucket: string;
        format: string;
        status: string;
      };

      const { rows } = await pool.query<DocRow>(
        'SELECT id, s3_key, s3_bucket, format, status FROM documents WHERE id = $1',
        [id],
      );

      if (rows.length === 0) {
        return reply.status(404).send({ error: 'Document not found' });
      }

      const doc = rows[0];

      if (doc.status !== 'active') {
        return reply.status(410).send({ error: 'Document is no longer available' });
      }

      // Only serve HTML content inline — other formats should use /download
      if (doc.format !== 'html') {
        return reply.status(400).send({
          error: 'Content endpoint only supports HTML documents. Use /download for other formats.',
        });
      }

      try {
        const command = new GetObjectCommand({
          Bucket: doc.s3_bucket,
          Key: doc.s3_key,
        });

        const response = await s3.send(command);

        if (!response.Body) {
          return reply.status(404).send({ error: 'Document content is empty' });
        }

        // Check content size
        if (response.ContentLength && response.ContentLength > MAX_CONTENT_SIZE) {
          return reply.status(413).send({
            error: 'Document is too large for inline viewing. Use /download instead.',
          });
        }

        // Read the full body into a buffer
        const chunks: Buffer[] = [];
        const stream = response.Body as Readable;
        for await (const chunk of stream) {
          chunks.push(Buffer.from(chunk));
        }
        const buffer = Buffer.concat(chunks);

        // Detect charset from the HTML content, using a preliminary UTF-8 parse
        // for the meta tag scan. This works even for non-UTF-8 content because
        // meta tags use ASCII-compatible bytes.
        const preliminaryText = buffer.toString('utf-8');
        const charset = detectCharset(preliminaryText);

        // Transcode to UTF-8 if a non-UTF-8 charset was detected
        let htmlContent: string;
        if (charset && charset !== 'utf-8' && charset !== 'utf8') {
          htmlContent = transcodeToUtf8(buffer, charset);
        } else {
          htmlContent = preliminaryText;
        }

        reply.header('Content-Type', 'text/html; charset=utf-8');
        reply.header('Cache-Control', 'public, max-age=3600');
        return reply.send(htmlContent);
      } catch (err) {
        if (
          err instanceof NoSuchKey ||
          (err instanceof Error && (err.name === 'NoSuchKey' || err.name === 'NotFound'))
        ) {
          req.log.warn(
            { documentId: id, bucket: doc.s3_bucket, key: doc.s3_key },
            'Document file not found in S3',
          );
          return reply.status(404).send({ error: 'Document file not found in storage' });
        }

        req.log.error(
          { err, documentId: id, bucket: doc.s3_bucket, key: doc.s3_key },
          'Failed to fetch document content from S3',
        );
        return reply.status(500).send({ error: 'Failed to fetch document content' });
      }
    },
  );
}
