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
 * Windows-1252 to Unicode mapping for the 0x80-0x9F range.
 * These bytes differ from ISO-8859-1 / Latin-1. Latin-1 maps them to
 * C1 control characters, but Windows-1252 maps them to printable characters.
 */
const WIN1252_MAP: Record<number, number> = {
  0x80: 0x20ac, // Euro sign
  0x82: 0x201a, // Single low-9 quotation mark
  0x83: 0x0192, // Latin small f with hook
  0x84: 0x201e, // Double low-9 quotation mark
  0x85: 0x2026, // Horizontal ellipsis
  0x86: 0x2020, // Dagger
  0x87: 0x2021, // Double dagger
  0x88: 0x02c6, // Modifier letter circumflex accent
  0x89: 0x2030, // Per mille sign
  0x8a: 0x0160, // Latin capital S with caron
  0x8b: 0x2039, // Single left-pointing angle quotation mark
  0x8c: 0x0152, // Latin capital ligature OE
  0x8e: 0x017d, // Latin capital Z with caron
  0x91: 0x2018, // Left single quotation mark
  0x92: 0x2019, // Right single quotation mark
  0x93: 0x201c, // Left double quotation mark
  0x94: 0x201d, // Right double quotation mark
  0x95: 0x2022, // Bullet
  0x96: 0x2013, // En dash
  0x97: 0x2014, // Em dash
  0x98: 0x02dc, // Small tilde
  0x99: 0x2122, // Trade mark sign
  0x9a: 0x0161, // Latin small s with caron
  0x9b: 0x203a, // Single right-pointing angle quotation mark
  0x9c: 0x0153, // Latin small ligature oe
  0x9e: 0x017e, // Latin small z with caron
  0x9f: 0x0178, // Latin capital Y with diaeresis
};

/**
 * Transcode a Buffer from a given charset to UTF-8.
 *
 * Uses a manual mapping for Windows-1252 (the most common legacy charset in
 * court HTML) because Node.js's TextDecoder may not support it without full
 * ICU data. Falls back to TextDecoder for other charsets, then to latin1.
 */
export function transcodeToUtf8(buffer: Buffer, charset: string): string {
  const lower = charset.toLowerCase().replace(/[^a-z0-9]/g, '');

  // Windows-1252: use manual byte-by-byte mapping.
  // Build the string per-character to avoid spreading a large array as function
  // arguments (V8 limits spread to ~65 536 args, but documents can be up to 5 MB).
  if (lower === 'windows1252' || lower === 'cp1252' || lower === 'win1252') {
    const parts: string[] = new Array(buffer.length);
    for (let i = 0; i < buffer.length; i++) {
      const byte = buffer[i];
      const mapped = WIN1252_MAP[byte];
      parts[i] = String.fromCharCode(mapped !== undefined ? mapped : byte);
    }
    return parts.join('');
  }

  // Try TextDecoder for other charsets (iso-8859-1, etc.)
  try {
    const decoder = new TextDecoder(charset, { fatal: true });
    return decoder.decode(buffer);
  } catch {
    // If the charset is not supported by TextDecoder, fall back to latin1
    return buffer.toString('latin1');
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

        // Read the full body into a buffer with a streaming size guard.
        // ContentLength may be absent (e.g. Transfer-Encoding: chunked),
        // so we also enforce the limit while reading chunks.
        const chunks: Buffer[] = [];
        const stream = response.Body as Readable;
        let totalBytes = 0;
        let payloadTooLarge = false;

        try {
          for await (const chunk of stream) {
            totalBytes += chunk.length;
            if (totalBytes > MAX_CONTENT_SIZE) {
              payloadTooLarge = true;
              stream.destroy();
              break;
            }
            chunks.push(chunk as Buffer);
          }
        } catch (streamErr) {
          // When we intentionally destroy the stream, an AbortError is expected
          // from the for-await loop. Any other error should be re-thrown.
          if (!payloadTooLarge || (streamErr instanceof Error && streamErr.name !== 'AbortError')) {
            throw streamErr;
          }
        }

        if (payloadTooLarge) {
          return reply.status(413).send({
            error: 'Document is too large for inline viewing. Use /download instead.',
          });
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
