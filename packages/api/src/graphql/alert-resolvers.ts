import type { Pool } from 'pg';
import { GraphQLError } from 'graphql';
import type { AuthUser } from '../auth';

type Row = Record<string, unknown>;

interface AlertContext {
  pool: Pool;
  user: AuthUser | null;
}

const VALID_ALERT_TYPES = ['case_docket', 'judge_ruling', 'keyword', 'party_attorney'];

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

      if (!VALID_ALERT_TYPES.includes(alertType)) {
        throw new GraphQLError(
          `Invalid alert type: ${alertType}. Must be one of: ${VALID_ALERT_TYPES.join(', ')}`,
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }

      // Validate filters is valid JSON
      let parsedFilters: Record<string, unknown>;
      try {
        parsedFilters = JSON.parse(filters) as Record<string, unknown>;
      } catch {
        throw new GraphQLError('filters must be a valid JSON string', {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }

      const { rows } = await pool.query<Row>(
        `INSERT INTO alert_subscriptions (user_id, alert_type, filters)
         VALUES ($1, $2, $3)
         RETURNING *`,
        [authed.id, alertType, JSON.stringify(parsedFilters)],
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
