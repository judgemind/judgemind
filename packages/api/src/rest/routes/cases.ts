import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { pageSize, decodeCursor, buildPage } from '../pagination';

type Row = Record<string, unknown>;

const caseSchema = {
  type: 'object',
  properties: {
    id: { type: 'string', format: 'uuid' },
    case_number: { type: 'string' },
    case_title: { type: 'string', nullable: true },
    case_type: { type: 'string', enum: ['civil', 'criminal', 'family', 'probate'], nullable: true },
    case_status: { type: 'string', enum: ['active', 'closed', 'dismissed'], nullable: true },
    court_id: { type: 'string', format: 'uuid', nullable: true },
    filed_at: { type: 'string', nullable: true },
    created_at: { type: 'string' },
  },
} as const;

const paginationSchema = {
  type: 'object',
  properties: {
    has_more: { type: 'boolean' },
    next_cursor: { type: 'string', nullable: true },
  },
} as const;

export async function casesRoutes(fastify: FastifyInstance, options: { pool: Pool }): Promise<void> {
  const { pool } = options;

  // GET /v1/cases
  fastify.get(
    '/cases',
    {
      schema: {
        tags: ['Cases'],
        summary: 'List cases',
        description: 'Returns a paginated list of cases. All filters are optional.',
        querystring: {
          type: 'object',
          properties: {
            court_id: { type: 'string', format: 'uuid', description: 'Filter by court ID' },
            case_status: {
              type: 'string',
              enum: ['active', 'closed', 'dismissed'],
              description: 'Filter by case status',
            },
            case_type: {
              type: 'string',
              enum: ['civil', 'criminal', 'family', 'probate'],
              description: 'Filter by case type',
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
              data: { type: 'array', items: caseSchema },
              pagination: paginationSchema,
            },
          },
        },
        security: [{ apiKey: [] }, {}],
      },
    },
    async (req) => {
      const query = req.query as {
        court_id?: string;
        case_status?: string;
        case_type?: string;
        limit?: number;
        after?: string;
      };

      const limit = pageSize(query.limit);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      if (query.court_id) {
        conditions.push(`court_id = $${i++}`);
        params.push(query.court_id);
      }
      if (query.case_status) {
        conditions.push(`case_status = $${i++}`);
        params.push(query.case_status);
      }
      if (query.case_type) {
        conditions.push(`case_type = $${i++}`);
        params.push(query.case_type);
      }
      if (query.after) {
        const [createdAt, id] = decodeCursor(query.after);
        conditions.push(`(created_at, id) < ($${i++}::timestamptz, $${i++}::uuid)`);
        params.push(createdAt, id);
      }

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT * FROM cases ${where} ORDER BY created_at DESC, id DESC LIMIT $${i}`,
        params,
      );

      return buildPage(rows, limit, (row) => [String(row.created_at), String(row.id)]);
    },
  );

  // GET /v1/cases/:id
  fastify.get(
    '/cases/:id',
    {
      schema: {
        tags: ['Cases'],
        summary: 'Get case by ID',
        params: {
          type: 'object',
          required: ['id'],
          properties: { id: { type: 'string', format: 'uuid' } },
        },
        response: {
          200: {
            type: 'object',
            properties: { data: caseSchema },
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
      const { rows } = await pool.query<Row>('SELECT * FROM cases WHERE id = $1', [id]);
      if (!rows[0]) {
        return reply.status(404).send({ statusCode: 404, error: 'Not Found', message: 'Case not found' });
      }
      return { data: rows[0] };
    },
  );
}
