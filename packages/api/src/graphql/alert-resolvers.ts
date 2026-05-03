import type { Pool } from 'pg';
import { z } from 'zod';
import { GraphQLError } from 'graphql';
import type { AuthUser } from '../auth';

type Row = Record<string, unknown>;

interface AlertContext {
  pool: Pool;
  user: AuthUser | null;
}

const MAX_FILTERS_LEN = 4096;

const filterSchemas = {
  judge_ruling:   z.object({ judge_id: z.string().uuid() }).strict(),
  case_docket:    z.object({ case_id:  z.string().uuid() }).strict(),
  keyword:        z.object({ keyword:  z.string().min(1).max(200) }).strict(),
  party_attorney: z.object({ party_id: z.string().uuid() }).strict(),
} as const;

const VALID_ALERT_TYPES = Object.keys(filterSchemas);

function requireAuth(user: AuthUser | null): AuthUser {
  if (!user) {
    throw new GraphQLError('Not authenticated', {
      extensions: { code: 'UNAUTHENTICATED' },
    });
  }
  return user;
}

export const alertResolvers = {
  Query: {
    myAlerts: async (_: unknown, __: unknown, { pool, user }: AlertContext) => {
      const authed = requireAuth(user);
      const { rows } = await pool.query<Row>(
        `SELECT * FROM alert_subscriptions WHERE user_id = $1 ORDER BY created_at DESC`,
        [authed.id],
      );
      return rows;
    },
  },

  Mutation: {
    createAlertSubscription: async (
      _: unknown,
      { alertType, filters }: { alertType: string; filters: string },
      { pool, user }: AlertContext,
    ) => {
      const authed = requireAuth(user);

      if (filters.length > MAX_FILTERS_LEN) {
        throw new GraphQLError(
          `filters must not exceed ${MAX_FILTERS_LEN} characters`,
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }

      if (!VALID_ALERT_TYPES.includes(alertType)) {
        throw new GraphQLError(
          `Invalid alert type: ${alertType}. Must be one of: ${VALID_ALERT_TYPES.join(', ')}`,
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }

      // Validate filters is valid JSON
      let rawParsed: unknown;
      try {
        rawParsed = JSON.parse(filters);
      } catch {
        throw new GraphQLError('filters must be a valid JSON string', {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }

      // Validate filters shape against per-alert-type strict Zod schema
      const schema = filterSchemas[alertType as keyof typeof filterSchemas];
      const result = schema.safeParse(rawParsed);
      if (!result.success) {
        throw new GraphQLError(result.error.issues[0].message, {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }

      const { rows } = await pool.query<Row>(
        `INSERT INTO alert_subscriptions (user_id, alert_type, filters)
         VALUES ($1, $2, $3)
         RETURNING *`,
        [authed.id, alertType, JSON.stringify(result.data)],
      );

      return rows[0];
    },

    deleteAlertSubscription: async (
      _: unknown,
      { id }: { id: string },
      { pool, user }: AlertContext,
    ) => {
      const authed = requireAuth(user);

      const { rowCount } = await pool.query(
        `DELETE FROM alert_subscriptions WHERE id = $1 AND user_id = $2`,
        [id, authed.id],
      );

      if ((rowCount ?? 0) === 0) {
        throw new GraphQLError('Subscription not found or not owned by you', {
          extensions: { code: 'NOT_FOUND' },
        });
      }

      return true;
    },

    toggleAlertSubscription: async (
      _: unknown,
      { id, isActive }: { id: string; isActive: boolean },
      { pool, user }: AlertContext,
    ) => {
      const authed = requireAuth(user);

      const { rows } = await pool.query<Row>(
        `UPDATE alert_subscriptions SET is_active = $1
         WHERE id = $2 AND user_id = $3
         RETURNING *`,
        [isActive, id, authed.id],
      );

      if (rows.length === 0) {
        throw new GraphQLError('Subscription not found or not owned by you', {
          extensions: { code: 'NOT_FOUND' },
        });
      }

      return rows[0];
    },
  },

  AlertSubscription: {
    alertType: (row: Row) => row.alert_type,
    filters: (row: Row) => JSON.stringify(row.filters),
    isActive: (row: Row) => row.is_active,
    createdAt: (row: Row) => row.created_at,
  },
};
