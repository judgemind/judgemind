import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { pageSize, decodeCursor, buildPage } from '../pagination';

type Row = Record<string, unknown>;

const rulingSchema = {
  type: 'object',
  properties: {
    id: { type: 'string', format: 'uuid' },
    document_id: { type: 'string', format: 'uuid', nullable: true },
    case_id: { type: 'string', format: 'uuid', nullable: true },
    judge_id: { type: 'string', format: 'uuid', nullable: true },
    court_id: { type: 'string', format: 'uuid', nullable: true },
    outcome: { type: 'string', nullable: true },
    motion_type: { type: 'string', nullable: true },
    hearing_date: { type: 'string', nullable: true },
    posted_at: { type: 'string', nullable: true },
    department: { type: 'string', nullable: true },
    is_tentative: { type: 'boolean' },
    ruling_number: { type: 'string', nullable: true },
    summary: { type: 'string', nullable: true },
    ruling_text: { type: 'string', nullable: true },
  },
} as const;

const paginationSchema = {
  type: 'object',
  properties: {
    has_more: { type: 'boolean' },
    next_cursor: { type: 'string', nullable: true },
  },
} as const;

export async function rulingsRoutes(
  fastify: FastifyInstance,
  options: { pool: Pool },
): Promise<void> {
  const { pool } = options;

  // GET /v1/rulings
  fastify.get(
    '/rulings',
    {
      schema: {
        tags: ['Rulings'],
        summary: 'List rulings',
        description:
          'Returns a paginated list of tentative and final rulings, ordered by hearing date (newest first).',
        querystring: {
          type: 'object',
          properties: {
            judge_id: { type: 'string', format: 'uuid', description: 'Filter by judge ID' },
            case_id: { type: 'string', format: 'uuid', description: 'Filter by case ID' },
            court_id: { type: 'string', format: 'uuid', description: 'Filter by court ID' },
            county: { type: 'string', description: 'Filter by county name' },
            outcome: {
              type: 'string',
              enum: [
                'granted',
                'denied',
                'granted_in_part',
                'denied_in_part',
                'moot',
                'continued',
                'off_calendar',
                'submitted',
                'other',
              ],
              description: 'Filter by ruling outcome',
            },
            date_from: {
              type: 'string',
              description: 'Filter rulings on or after this hearing date (ISO 8601)',
            },
            date_to: {
              type: 'string',
              description: 'Filter rulings on or before this hearing date (ISO 8601)',
            },
            case_number: { type: 'string', description: 'Filter by exact case number' },
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
              data: { type: 'array', items: rulingSchema },
              pagination: paginationSchema,
            },
          },
        },
        security: [{ apiKey: [] }, {}],
      },
    },
    async (req) => {
      const query = req.query as {
        judge_id?: string;
        case_id?: string;
        court_id?: string;
        county?: string;
        outcome?: string;
        date_from?: string;
        date_to?: string;
        case_number?: string;
        limit?: number;
        after?: string;
      };

      const limit = pageSize(query.limit);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      if (query.judge_id) {
        conditions.push(`r.judge_id = $${i++}`);
        params.push(query.judge_id);
      }
      if (query.case_id) {
        conditions.push(`r.case_id = $${i++}`);
        params.push(query.case_id);
      }
      if (query.court_id) {
        conditions.push(`r.court_id = $${i++}`);
        params.push(query.court_id);
      }
      if (query.county) {
        conditions.push(`ct.county = $${i++}`);
        params.push(query.county);
      }
      if (query.outcome) {
        conditions.push(`r.outcome = $${i++}`);
        params.push(query.outcome);
      }
      if (query.date_from) {
        conditions.push(`r.hearing_date >= $${i++}`);
        params.push(query.date_from);
      }
      if (query.date_to) {
        conditions.push(`r.hearing_date <= $${i++}`);
        params.push(query.date_to);
      }
      if (query.case_number) {
        conditions.push(`cs.case_number = $${i++}`);
        params.push(query.case_number);
      }
      if (query.after) {
        const [hearingDate, id] = decodeCursor(query.after);
        conditions.push(`(r.hearing_date, r.id) < ($${i++}::date, $${i++}::uuid)`);
        params.push(hearingDate, id);
      }

      // Only JOIN tables when their columns are needed for filtering
      const joins = [
        query.county !== undefined ? 'JOIN courts ct ON ct.id = r.court_id' : '',
        query.case_number !== undefined ? 'JOIN cases cs ON cs.id = r.case_id' : '',
      ]
        .filter(Boolean)
        .join(' ');

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT r.* FROM rulings r ${joins} ${where} ORDER BY r.hearing_date DESC, r.id DESC LIMIT $${i}`,
        params,
      );

      return buildPage(rows, limit, (row) => [String(row.hearing_date), String(row.id)]);
    },
  );

  // GET /v1/rulings/:id
  fastify.get(
    '/rulings/:id',
    {
      schema: {
        tags: ['Rulings'],
        summary: 'Get ruling by ID',
        params: {
          type: 'object',
          required: ['id'],
          properties: { id: { type: 'string', format: 'uuid' } },
        },
        response: {
          200: {
            type: 'object',
            properties: { data: rulingSchema },
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
      const { rows } = await pool.query<Row>('SELECT * FROM rulings WHERE id = $1', [id]);
      if (!rows[0]) {
        return reply
          .status(404)
          .send({ statusCode: 404, error: 'Not Found', message: 'Ruling not found' });
      }
      return { data: rows[0] };
    },
  );
}
