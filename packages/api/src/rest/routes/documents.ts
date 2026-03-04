import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { pageSize, decodeCursor, buildPage } from '../pagination';

type Row = Record<string, unknown>;

const documentSchema = {
  type: 'object',
  properties: {
    id: { type: 'string', format: 'uuid' },
    case_id: { type: 'string', format: 'uuid', nullable: true },
    court_id: { type: 'string', format: 'uuid', nullable: true },
    document_type: { type: 'string', nullable: true },
    motion_type: { type: 'string', nullable: true },
    s3_key: { type: 'string' },
    s3_bucket: { type: 'string' },
    format: { type: 'string' },
    content_hash: { type: 'string' },
    source_url: { type: 'string', nullable: true },
    scraper_id: { type: 'string', nullable: true },
    captured_at: { type: 'string' },
    hearing_date: { type: 'string', nullable: true },
    status: { type: 'string' },
  },
} as const;

const paginationSchema = {
  type: 'object',
  properties: {
    has_more: { type: 'boolean' },
    next_cursor: { type: 'string', nullable: true },
  },
} as const;

export async function documentsRoutes(
  fastify: FastifyInstance,
  options: { pool: Pool },
): Promise<void> {
  const { pool } = options;

  // GET /v1/documents
  fastify.get(
    '/documents',
    {
      schema: {
        tags: ['Documents'],
        summary: 'List documents',
        description: 'Returns a paginated list of documents, ordered by capture time (newest first).',
        querystring: {
          type: 'object',
          properties: {
            case_id: { type: 'string', format: 'uuid', description: 'Filter by case ID' },
            court_id: { type: 'string', format: 'uuid', description: 'Filter by court ID' },
            document_type: {
              type: 'string',
              enum: ['ruling', 'motion', 'brief', 'order', 'docket_entry'],
              description: 'Filter by document type',
            },
            status: {
              type: 'string',
              enum: ['active', 'superseded', 'removed'],
              description: 'Filter by document status',
            },
            limit: {
              type: 'integer',
              minimum: 1,
              maximum: 100,
              default: 20,
              description: 'Number of results per page (max 100)',
            },
            after: { type: 'string', description: 'Pagination cursor from previous response' },
          },
        },
        response: {
          200: {
            type: 'object',
            properties: {
              data: { type: 'array', items: documentSchema },
              pagination: paginationSchema,
            },
          },
        },
        security: [{ apiKey: [] }, {}],
      },
    },
    async (req) => {
      const query = req.query as {
        case_id?: string;
        court_id?: string;
        document_type?: string;
        status?: string;
        limit?: number;
        after?: string;
      };

      const limit = pageSize(query.limit);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      if (query.case_id) {
        conditions.push(`case_id = $${i++}`);
        params.push(query.case_id);
      }
      if (query.court_id) {
        conditions.push(`court_id = $${i++}`);
        params.push(query.court_id);
      }
      if (query.document_type) {
        conditions.push(`document_type = $${i++}`);
        params.push(query.document_type);
      }
      if (query.status) {
        conditions.push(`status = $${i++}`);
        params.push(query.status);
      }
      if (query.after) {
        const [capturedAt, id] = decodeCursor(query.after);
        conditions.push(`(captured_at, id) < ($${i++}::timestamptz, $${i++}::uuid)`);
        params.push(capturedAt, id);
      }

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT * FROM documents ${where} ORDER BY captured_at DESC, id DESC LIMIT $${i}`,
        params,
      );

      return buildPage(rows, limit, (row) => [String(row.captured_at), String(row.id)]);
    },
  );

  // GET /v1/documents/:id
  fastify.get(
    '/documents/:id',
    {
      schema: {
        tags: ['Documents'],
        summary: 'Get document by ID',
        params: {
          type: 'object',
          required: ['id'],
          properties: { id: { type: 'string', format: 'uuid' } },
        },
        response: {
          200: {
            type: 'object',
            properties: { data: documentSchema },
          },
          404: {
            type: 'object',
            properties: { statusCode: { type: 'integer' }, error: { type: 'string' }, message: { type: 'string' } },
          },
        },
        security: [{ apiKey: [] }, {}],
      },
    },
    async (req, reply) => {
      const { id } = req.params as { id: string };
      const { rows } = await pool.query<Row>('SELECT * FROM documents WHERE id = $1', [id]);
      if (!rows[0]) {
        return reply
          .status(404)
          .send({ statusCode: 404, error: 'Not Found', message: 'Document not found' });
      }
      return { data: rows[0] };
    },
  );
}
