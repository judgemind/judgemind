import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { pageSize, decodeCursor, buildPage } from '../pagination';

type Row = Record<string, unknown>;

const attorneySchema = {
  type: 'object',
  properties: {
    id: { type: 'string', format: 'uuid' },
    canonical_name: { type: 'string' },
    bar_number: { type: 'string', nullable: true },
    bar_state: { type: 'string', nullable: true },
    firm_name: { type: 'string', nullable: true },
    is_active: { type: 'boolean' },
  },
} as const;

const paginationSchema = {
  type: 'object',
  properties: {
    has_more: { type: 'boolean' },
    next_cursor: { type: 'string', nullable: true },
  },
} as const;

export async function attorneysRoutes(
  fastify: FastifyInstance,
  options: { pool: Pool },
): Promise<void> {
  const { pool } = options;

  // GET /v1/attorneys
  fastify.get(
    '/attorneys',
    {
      schema: {
        tags: ['Attorneys'],
        summary: 'List attorneys',
        description: 'Returns a paginated list of attorneys ordered by name.',
        querystring: {
          type: 'object',
          properties: {
            bar_state: { type: 'string', description: 'Filter by bar state (e.g. "CA")' },
            firm_name: { type: 'string', description: 'Filter by firm name (exact match)' },
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
              data: { type: 'array', items: attorneySchema },
              pagination: paginationSchema,
            },
          },
        },
        security: [{ apiKey: [] }, {}],
      },
    },
    async (req) => {
      const query = req.query as {
        bar_state?: string;
        firm_name?: string;
        is_active?: boolean;
        limit?: number;
        after?: string;
      };

      const limit = pageSize(query.limit);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      if (query.bar_state) {
        conditions.push(`bar_state = $${i++}`);
        params.push(query.bar_state);
      }
      if (query.firm_name) {
        conditions.push(`firm_name = $${i++}`);
        params.push(query.firm_name);
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
        `SELECT id, canonical_name, bar_number, bar_state, firm_name, is_active FROM attorneys ${where} ORDER BY canonical_name ASC, id ASC LIMIT $${i}`,
        params,
      );

      return buildPage(rows, limit, (row) => [String(row.canonical_name), String(row.id)]);
    },
  );

  // GET /v1/attorneys/:id
  fastify.get(
    '/attorneys/:id',
    {
      schema: {
        tags: ['Attorneys'],
        summary: 'Get attorney by ID',
        params: {
          type: 'object',
          required: ['id'],
          properties: { id: { type: 'string', format: 'uuid' } },
        },
        response: {
          200: {
            type: 'object',
            properties: { data: attorneySchema },
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
      const { rows } = await pool.query<Row>(
        'SELECT id, canonical_name, bar_number, bar_state, firm_name, is_active FROM attorneys WHERE id = $1',
        [id],
      );
      if (!rows[0]) {
        return reply
          .status(404)
          .send({ statusCode: 404, error: 'Not Found', message: 'Attorney not found' });
      }
      return { data: rows[0] };
    },
  );
}
