import type { Pool } from 'pg';
import type { FastifyReply } from 'fastify';
import type { Client } from '@opensearch-project/opensearch';
import type { Loaders } from './dataloader';
import type { AuthUser } from '../auth';
import { authResolvers } from './auth-resolvers';
import { alertResolvers } from './alert-resolvers';
import { dataQualityResolvers } from './data-quality';
import { searchRulings } from '../search/search-rulings';
import { getJudgeAnalytics, getMultipleJudgeAnalytics } from './judge-analytics';
import { getPlatformStats } from './platform-stats-cache';

interface Context {
  pool: Pool;
  loaders: Loaders;
  user: AuthUser | null;
  ip: string;
  reply: FastifyReply;
  cookieHeader: string;
  opensearch: Client;
}

type Row = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Cursor helpers
// Cursors are opaque base64 strings that encode the ordered columns used
// for keyset pagination, separated by "|".
// ---------------------------------------------------------------------------

function encodeCursor(parts: string[]): string {
  return Buffer.from(parts.join('|')).toString('base64');
}

function decodeCursor(cursor: string): string[] {
  return Buffer.from(cursor, 'base64').toString('utf8').split('|');
}

/** Clamp page size: default 20, max 100. */
function pageSize(first: number | undefined | null): number {
  const n = first ?? 20;
  return Math.min(Math.max(1, n), 100);
}

// ---------------------------------------------------------------------------
// Resolvers
// ---------------------------------------------------------------------------

export const resolvers = {
  Query: {
    health: () => 'ok',

    // -----------------------------------------------------------------------
    // platformStats — aggregate counts for the home page
    // -----------------------------------------------------------------------

    platformStats: async (_: unknown, __: unknown, { pool }: Context) => {
      return getPlatformStats(pool);
    },

    // -----------------------------------------------------------------------
    // distinctCounties / distinctJudgeNames — lightweight autocomplete lists
    // -----------------------------------------------------------------------

    distinctCounties: async (_: unknown, __: unknown, { pool }: Context) => {
      const { rows } = await pool.query<{ county: string }>(
        `SELECT DISTINCT county FROM courts WHERE is_active = true ORDER BY county ASC`,
      );
      return rows.map((r) => r.county);
    },

    distinctJudgeNames: async (_: unknown, __: unknown, { pool }: Context) => {
      const { rows } = await pool.query<{ canonical_name: string }>(
        `SELECT DISTINCT canonical_name FROM judges WHERE is_active = true ORDER BY canonical_name ASC`,
      );
      return rows.map((r) => r.canonical_name);
    },

    // -----------------------------------------------------------------------
    // searchRulings — full-text + filtered search via OpenSearch
    // -----------------------------------------------------------------------

    searchRulings: async (
      _: unknown,
      {
        query,
        filters,
        first,
        after,
        includeFuture,
      }: {
        query?: string;
        filters?: {
          court?: string;
          county?: string;
          state?: string;
          judgeName?: string;
          dateFrom?: string;
          dateTo?: string;
          caseNumber?: string;
          motionTypes?: string[];
          outcomes?: string[];
          caseTypes?: string[];
        };
        first?: number;
        after?: string;
        includeFuture?: boolean;
      },
      { opensearch, pool }: Context,
    ) => {
      return searchRulings(opensearch, pool, { query, filters, first, after, includeFuture });
    },

    // -----------------------------------------------------------------------
    // case / cases
    // -----------------------------------------------------------------------

    case: async (_: unknown, { id }: { id: string }, { pool }: Context) => {
      const { rows } = await pool.query<Row>('SELECT * FROM cases WHERE id = $1', [id]);
      return rows[0] ?? null;
    },

    cases: async (
      _: unknown,
      {
        courtId,
        caseStatus,
        caseType,
        county,
        dateFrom,
        dateTo,
        outcome,
        motionType,
        first,
        after,
      }: {
        courtId?: string;
        caseStatus?: string;
        caseType?: string;
        county?: string;
        dateFrom?: string;
        dateTo?: string;
        outcome?: string;
        motionType?: string;
        first?: number;
        after?: string;
      },
      { pool }: Context,
    ) => {
      const limit = pageSize(first);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      if (courtId) {
        conditions.push(`c.court_id = $${i++}`);
        params.push(courtId);
      }
      if (caseStatus) {
        conditions.push(`c.case_status = $${i++}`);
        params.push(caseStatus);
      }
      if (caseType) {
        conditions.push(`c.case_type = $${i++}`);
        params.push(caseType);
      }
      if (county) {
        conditions.push(`ct.county = $${i++}`);
        params.push(county);
      }

      // Use an EXISTS subquery for ruling-level filters (dates, outcome, motion
      // type) instead of JOIN + DISTINCT. This is more efficient: the DB stops
      // checking each case as soon as it finds one matching ruling.
      const rulingConditions: string[] = [];
      if (dateFrom) {
        rulingConditions.push(`r.hearing_date >= $${i++}`);
        params.push(dateFrom);
      }
      if (dateTo) {
        rulingConditions.push(`r.hearing_date <= $${i++}`);
        params.push(dateTo);
      }
      if (outcome) {
        rulingConditions.push(`r.outcome = $${i++}`);
        params.push(outcome);
      }
      if (motionType) {
        rulingConditions.push(`r.motion_type = $${i++}`);
        params.push(motionType);
      }
      if (rulingConditions.length > 0) {
        // Exclude future hearing dates by default for consistency with the
        // rulings resolver (which also excludes them unless includeFuture is set).
        conditions.push(
          `EXISTS (SELECT 1 FROM rulings r WHERE r.case_id = c.id AND r.hearing_date <= CURRENT_DATE AND ${rulingConditions.join(' AND ')})`,
        );
      }

      // Keyset pagination — order by (created_at DESC, id DESC)
      // Cursor encodes [created_at, id]
      if (after) {
        const [createdAt, id] = decodeCursor(after);
        conditions.push(`(c.created_at, c.id) < ($${i++}::timestamptz, $${i++}::uuid)`);
        params.push(createdAt, id);
      }

      // Only JOIN courts when county filter is active
      const joins = county !== undefined ? 'JOIN courts ct ON ct.id = c.court_id' : '';

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT c.* FROM cases c ${joins} ${where} ORDER BY c.created_at DESC, c.id DESC LIMIT $${i}`,
        params,
      );

      const hasNextPage = rows.length > limit;
      const edges = rows.slice(0, limit);
      return {
        edges: edges.map((row) => ({
          node: row,
          cursor: encodeCursor([String(row.created_at), String(row.id)]),
        })),
        pageInfo: {
          hasNextPage,
          endCursor:
            edges.length > 0
              ? encodeCursor([
                  String(edges[edges.length - 1].created_at),
                  String(edges[edges.length - 1].id),
                ])
              : null,
        },
      };
    },

    // -----------------------------------------------------------------------
    // judgeAnalytics
    // -----------------------------------------------------------------------

    judgeAnalytics: async (
      _: unknown,
      { judgeId }: { judgeId: string },
      { pool }: Context,
    ) => {
      return getJudgeAnalytics(pool, judgeId);
    },

    compareJudges: async (
      _: unknown,
      { judgeIds }: { judgeIds: string[] },
      { pool }: Context,
    ) => {
      const results = await getMultipleJudgeAnalytics(pool, judgeIds);
      // Filter out null entries (non-existent judges)
      return results.filter((r): r is NonNullable<typeof r> => r !== null);
    },

    // -----------------------------------------------------------------------
    // judge / judges
    // -----------------------------------------------------------------------

    judge: async (_: unknown, { id }: { id: string }, { pool }: Context) => {
      const { rows } = await pool.query<Row>('SELECT * FROM judges WHERE id = $1', [id]);
      return rows[0] ?? null;
    },

    judges: async (
      _: unknown,
      {
        courtId,
        county,
        first,
        after,
      }: { courtId?: string; county?: string; first?: number; after?: string },
      { pool }: Context,
    ) => {
      const limit = pageSize(first);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      if (courtId) {
        conditions.push(`j.court_id = $${i++}`);
        params.push(courtId);
      }
      if (county) {
        conditions.push(`ct.county = $${i++}`);
        params.push(county);
      }

      // Keyset — order by (canonical_name ASC, id ASC)
      // Cursor encodes [canonical_name, id]
      if (after) {
        const [name, id] = decodeCursor(after);
        conditions.push(`(j.canonical_name, j.id) > ($${i++}, $${i++}::uuid)`);
        params.push(name, id);
      }

      const needCourtsJoin = county !== undefined;
      const joins = needCourtsJoin ? 'JOIN courts ct ON ct.id = j.court_id' : '';
      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      // ruling_count is resolved by the Judge.rulingCount field resolver via
      // judgeRulingCountLoader (batched), so we no longer need a correlated subquery here.
      const { rows } = await pool.query<Row>(
        `SELECT j.*
         FROM judges j ${joins}
         ${where}
         ORDER BY j.canonical_name ASC, j.id ASC
         LIMIT $${i}`,
        params,
      );

      const hasNextPage = rows.length > limit;
      const edges = rows.slice(0, limit);
      return {
        edges: edges.map((row) => ({
          node: row,
          cursor: encodeCursor([String(row.canonical_name), String(row.id)]),
        })),
        pageInfo: {
          hasNextPage,
          endCursor:
            edges.length > 0
              ? encodeCursor([
                  String(edges[edges.length - 1].canonical_name),
                  String(edges[edges.length - 1].id),
                ])
              : null,
        },
      };
    },

    // -----------------------------------------------------------------------
    // ruling / rulings
    // -----------------------------------------------------------------------

    ruling: async (_: unknown, { id }: { id: string }, { pool }: Context) => {
      const { rows } = await pool.query<Row>('SELECT * FROM rulings WHERE id = $1', [id]);
      return rows[0] ?? null;
    },

    rulings: async (
      _: unknown,
      {
        judgeId,
        caseId,
        courtId,
        county,
        outcome,
        motionType,
        caseType,
        dateFrom,
        dateTo,
        caseNumber,
        includeFuture,
        first,
        after,
      }: {
        judgeId?: string;
        caseId?: string;
        courtId?: string;
        county?: string;
        outcome?: string;
        motionType?: string;
        caseType?: string;
        dateFrom?: string;
        dateTo?: string;
        caseNumber?: string;
        includeFuture?: boolean;
        first?: number;
        after?: string;
      },
      { pool }: Context,
    ) => {
      const limit = pageSize(first);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      // Exclude future hearing dates by default
      if (!includeFuture) {
        conditions.push(`r.hearing_date <= CURRENT_DATE`);
      }

      if (judgeId) {
        conditions.push(`r.judge_id = $${i++}`);
        params.push(judgeId);
      }
      if (caseId) {
        conditions.push(`r.case_id = $${i++}`);
        params.push(caseId);
      }
      if (courtId) {
        conditions.push(`r.court_id = $${i++}`);
        params.push(courtId);
      }
      if (county) {
        conditions.push(`ct.county = $${i++}`);
        params.push(county);
      }
      if (outcome) {
        conditions.push(`r.outcome = $${i++}`);
        params.push(outcome);
      }
      if (motionType) {
        conditions.push(`r.motion_type = $${i++}`);
        params.push(motionType);
      }
      if (caseType) {
        conditions.push(`cs.case_type = $${i++}`);
        params.push(caseType);
      }
      if (dateFrom) {
        conditions.push(`r.hearing_date >= $${i++}`);
        params.push(dateFrom);
      }
      if (dateTo) {
        conditions.push(`r.hearing_date <= $${i++}`);
        params.push(dateTo);
      }
      if (caseNumber) {
        conditions.push(`cs.case_number = $${i++}`);
        params.push(caseNumber);
      }

      // Keyset — order by (hearing_date DESC, id DESC)
      // Cursor encodes [hearing_date, id]
      if (after) {
        const [hearingDate, id] = decodeCursor(after);
        conditions.push(`(r.hearing_date, r.id) < ($${i++}::date, $${i++}::uuid)`);
        params.push(hearingDate, id);
      }

      // Only JOIN tables when their columns are used in filters
      const needCasesJoin = caseNumber !== undefined || caseType !== undefined;
      const joins = [
        county !== undefined ? 'JOIN courts ct ON ct.id = r.court_id' : '',
        needCasesJoin ? 'JOIN cases cs ON cs.id = r.case_id' : '',
      ]
        .filter(Boolean)
        .join(' ');

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT r.* FROM rulings r ${joins} ${where} ORDER BY r.hearing_date DESC, r.id DESC LIMIT $${i}`,
        params,
      );

      const hasNextPage = rows.length > limit;
      const edges = rows.slice(0, limit);
      return {
        edges: edges.map((row) => ({
          node: row,
          cursor: encodeCursor([String(row.hearing_date), String(row.id)]),
        })),
        pageInfo: {
          hasNextPage,
          endCursor:
            edges.length > 0
              ? encodeCursor([
                  String(edges[edges.length - 1].hearing_date),
                  String(edges[edges.length - 1].id),
                ])
              : null,
        },
      };
    },
  },

  // -------------------------------------------------------------------------
  // Field resolvers — snake_case DB columns → camelCase GraphQL fields.
  // Court and Judge lookups use DataLoaders to prevent N+1 queries.
  // -------------------------------------------------------------------------

  Case: {
    caseNumber: (row: Row) => row.case_number,
    caseTitle: (row: Row) => row.case_title,
    caseType: (row: Row) => row.case_type,
    caseStatus: (row: Row) => row.case_status,
    filedAt: (row: Row) => row.filed_at,
    court: (row: Row, _: unknown, { loaders }: Context) =>
      row.court_id ? loaders.courtLoader.load(row.court_id as string) : null,
    judges: (row: Row, _: unknown, { loaders }: Context) =>
      loaders.caseJudgesLoader.load(row.id as string),
    parties: (row: Row, _: unknown, { loaders }: Context) =>
      loaders.casePartiesLoader.load(row.id as string),
    latestRuling: (row: Row, _: unknown, { loaders }: Context) =>
      loaders.latestRulingLoader.load(row.id as string),
  },

  Judge: {
    canonicalName: (row: Row) => row.canonical_name,
    isActive: (row: Row) => row.is_active,
    appointedAt: (row: Row) => row.appointed_at,
    rulingCount: (row: Row, _: unknown, { loaders }: Context) =>
      row.ruling_count !== undefined
        ? row.ruling_count
        : loaders.judgeRulingCountLoader.load(row.id as string),
    court: (row: Row, _: unknown, { loaders }: Context) =>
      row.court_id ? loaders.courtLoader.load(row.court_id as string) : null,
  },

  Court: {
    courtName: (row: Row) => row.court_name,
    courtCode: (row: Row) => row.court_code,
    isActive: (row: Row) => row.is_active,
  },

  Ruling: {
    hearingDate: (row: Row) => row.hearing_date,
    motionType: (row: Row) => row.motion_type,
    isTentative: (row: Row) => row.is_tentative,
    rulingText: (row: Row) => row.ruling_text,
    rulingTextHtml: (row: Row) => row.ruling_text_html,
    postedAt: (row: Row) => row.posted_at,
    documentId: (row: Row) => row.document_id,
    documentFormat: (row: Row, _: unknown, { loaders }: Context) =>
      row.document_id ? loaders.documentFormatLoader.load(row.document_id as string) : null,
    court: (row: Row, _: unknown, { loaders }: Context) =>
      row.court_id ? loaders.courtLoader.load(row.court_id as string) : null,
    judge: (row: Row, _: unknown, { loaders }: Context) =>
      row.judge_id ? loaders.judgeLoader.load(row.judge_id as string) : null,
    case: (row: Row, _: unknown, { loaders }: Context) =>
      row.case_id ? loaders.caseLoader.load(row.case_id as string) : null,
  },

  Document: {
    documentType: (row: Row) => row.document_type,
    motionType: (row: Row) => row.motion_type,
    s3Key: (row: Row) => row.s3_key,
    s3Bucket: (row: Row) => row.s3_bucket,
    contentHash: (row: Row) => row.content_hash,
    sourceUrl: (row: Row) => row.source_url,
    scraperId: (row: Row) => row.scraper_id,
    capturedAt: (row: Row) => row.captured_at,
    hearingDate: (row: Row) => row.hearing_date,
    court: (row: Row, _: unknown, { loaders }: Context) =>
      row.court_id ? loaders.courtLoader.load(row.court_id as string) : null,
    case: (row: Row, _: unknown, { loaders }: Context) =>
      row.case_id ? loaders.caseLoader.load(row.case_id as string) : null,
  },

  Party: {
    canonicalName: (row: Row) => row.canonical_name,
    partyType: (row: Row) => row.party_type,
    role: (row: Row) => row.role ?? null,
  },

  RulingSearchHit: {
    rulingId: (hit: Row) => hit.rulingId,
    caseNumber: (hit: Row) => hit.caseNumber,
    judgeName: (hit: Row) => hit.judgeName,
    hearingDate: (hit: Row) => hit.hearingDate,
  },

  // Alert field resolvers
  AlertSubscription: alertResolvers.AlertSubscription,

  // Auth resolvers
  ...authResolvers.User ? { User: authResolvers.User } : {},
  Mutation: authResolvers.Mutation,
};

// Merge auth Query resolvers into the main Query object
Object.assign(resolvers.Query, authResolvers.Query);

// Merge alert Query and Mutation resolvers
Object.assign(resolvers.Query, alertResolvers.Query);
Object.assign(resolvers.Mutation, alertResolvers.Mutation);

// Merge data quality resolvers
Object.assign(resolvers.Query, dataQualityResolvers.Query);
Object.assign(resolvers, {
  DataQualityMetric: dataQualityResolvers.DataQualityMetric,
  DataQualityOverview: dataQualityResolvers.DataQualityOverview,
});
