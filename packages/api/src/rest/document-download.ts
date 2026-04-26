import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { S3Client, GetObjectCommand, HeadObjectCommand, NoSuchKey } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';

/** Presigned URL TTL in seconds (15 minutes). */
const PRESIGNED_URL_TTL = 900;

/** Content-Type mapping for document formats. */
const FORMAT_CONTENT_TYPES: Record<string, string> = {
  pdf: 'application/pdf',
  html: 'text/html',
  txt: 'text/plain',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
};

/**
 * Register the document download REST endpoint.
 *
 * GET /api/documents/:id/download
 *
 * Looks up the document by ID, generates a presigned S3 URL, and redirects
 * the client to it. The presigned URL has a short TTL (15 min) so no S3
 * credentials are exposed to the client.
 */
export function registerDocumentDownload(
  app: FastifyInstance,
  pool: Pool,
  s3Client?: S3Client,
): void {
  const s3 = s3Client ?? new S3Client({
    region: process.env.AWS_REGION ?? 'us-west-2',
  });

  app.get<{ Params: { id: string } }>(
    '/api/documents/:id/download',
    async (req, reply) => {
      const { id } = req.params;

      // Validate UUID format to avoid SQL injection / unnecessary queries
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

      const contentType = FORMAT_CONTENT_TYPES[doc.format] ?? 'application/octet-stream';

      try {
        // Verify the object exists before generating a presigned URL.
        // getSignedUrl itself does not contact S3, so it cannot detect missing
        // objects — a HeadObject call catches that early and lets us return a
        // meaningful 404 instead of handing the client a URL that will 404 on
        // S3 with an opaque XML error.
        await s3.send(new HeadObjectCommand({
          Bucket: doc.s3_bucket,
          Key: doc.s3_key,
        }));

        const command = new GetObjectCommand({
          Bucket: doc.s3_bucket,
          Key: doc.s3_key,
          ResponseContentType: contentType,
        });

        const presignedUrl = await getSignedUrl(s3, command, {
          expiresIn: PRESIGNED_URL_TTL,
        });

        return reply.redirect(presignedUrl, 302);
      } catch (err) {
        if (err instanceof NoSuchKey
          || (err instanceof Error && (err.name === 'NoSuchKey' || err.name === 'NotFound'))) {
          req.log.warn({ documentId: id, bucket: doc.s3_bucket, key: doc.s3_key },
            'Document file not found in S3');
          return reply.status(404).send({ error: 'Document file not found in storage' });
        }

        req.log.error({ err, documentId: id, bucket: doc.s3_bucket, key: doc.s3_key },
          'Failed to generate presigned download URL');
        return reply.status(500).send({ error: 'Failed to generate download URL' });
      }
    },
  );
}
