import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { pageSize, decodeCursor, buildPage } from '../pagination';

type Row = Record<string, unknown>;

const judgeSchema = {
  type: 'object',
  properties: {
    id: { type: 'string', format: 'uuid' },
    canonical_name: { type: 'string' },
    court_id: { type: 'string', format: 'uuid', nullable: true },
    department: { type: 'string', nullable: true },
    is_active: { type: 'boolean' },
    appointed_at: { type: 'string', nullable: true },
  },
} as const;

const paginationSchema = {
  type: 'object',
  properties: {
    has_more: { type: 'boolean' },
    next_cursor: { type: 'string', nullable: true },
  },
} as const;

export async function judgesRoutes(
  fastify: FastifyInstance,
  options: { pool: Pool },
): Promise<void> {
  const { pool } = options;

  // GET /v1/judges
  fastify.get(
    '/judges',
    {
      schema: {
        tags: ['Judges'],
        summary: 'List judges',
        description: 'Returns a paginated list of judges ordered by name.',
        querystring: {
          type: 'object',
          properties: {
            court_id: { type: 'string', format: 'uuid', description: 'Filter by court ID' },
            is_active: { type: 'boolean', description: 'Filter by active status' },
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
              data: { type: 'array', items: judgeSchema },
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
        is_active?: boolean;
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
      if (query.is_active !== undefined) {
        conditions.push(`is_active = $${i++}`);
        params.push(query.is_active);
      }
      if (query.after) {
        const [name, id] = decodeCursor(query.after);
        conditions.push(`(canonical_name, id) > ($${i++}, $${i++}::uuid)`);
        params.push(name, id);
      }

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT * FROM judges ${where} ORDER BY canonical_name ASC, id ASC LIMIT $${i}`,
        params,
      );

      return buildPage(rows, limit, (row) => [String(row.canonical_name), String(row.id)]);
    },
  );

  // GET /v1/judges/:id
  fastify.get(
    '/judges/:id',
    {
      schema: {
        tags: ['Judges'],
        summary: 'Get judge by ID',
        params: {
          type: 'object',
          required: ['id'],
          properties: { id: { type: 'string', format: 'uuid' } },
        },
        response: {
          200: {
            type: 'object',
            properties: { data: judgeSchema },
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
      const { rows } = await pool.query<Row>('SELECT * FROM judges WHERE id = $1', [id]);
      if (!rows[0]) {
        return reply.status(404).send({ statusCode: 404, error: 'Not Found', message: 'Judge not found' });
      }
      return { data: rows[0] };
    },
  );
}
