import type { Pool } from 'pg';
import type { FastifyReply } from 'fastify';
import type { Client } from '@opensearch-project/opensearch';
import type { Loaders } from './dataloader';
import type { AuthUser } from '../auth';
import { authResolvers } from './auth-resolvers';
import { searchRulings } from '../search/search-rulings';
import { getJudgeAnalytics } from './judge-analytics';

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
        first,
        after,
      }: {
        courtId?: string;
        caseStatus?: string;
        caseType?: string;
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
        conditions.push(`court_id = $${i++}`);
        params.push(courtId);
      }
      if (caseStatus) {
        conditions.push(`case_status = $${i++}`);
        params.push(caseStatus);
      }
      if (caseType) {
        conditions.push(`case_type = $${i++}`);
        params.push(caseType);
      }

      // Keyset pagination — order by (created_at DESC, id DESC)
      // Cursor encodes [created_at, id]
      if (after) {
        const [createdAt, id] = decodeCursor(after);
        conditions.push(`(created_at, id) < ($${i++}::timestamptz, $${i++}::uuid)`);
        params.push(createdAt, id);
      }

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT * FROM cases ${where} ORDER BY created_at DESC, id DESC LIMIT $${i}`,
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

    // -----------------------------------------------------------------------
    // judge / judges
    // -----------------------------------------------------------------------

    judge: async (_: unknown, { id }: { id: string }, { pool }: Context) => {
      const { rows } = await pool.query<Row>('SELECT * FROM judges WHERE id = $1', [id]);
      return rows[0] ?? null;
    },

    judges: async (
      _: unknown,
      { courtId, first, after }: { courtId?: string; first?: number; after?: string },
      { pool }: Context,
    ) => {
      const limit = pageSize(first);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      if (courtId) {
        conditions.push(`court_id = $${i++}`);
        params.push(courtId);
      }

      // Keyset — order by (canonical_name ASC, id ASC)
      // Cursor encodes [canonical_name, id]
      if (after) {
        const [name, id] = decodeCursor(after);
        conditions.push(`(canonical_name, id) > ($${i++}, $${i++}::uuid)`);
        params.push(name, id);
      }

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(limit + 1);
      const { rows } = await pool.query<Row>(
        `SELECT * FROM judges ${where} ORDER BY canonical_name ASC, id ASC LIMIT $${i}`,
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
      const joins = [
        county !== undefined ? 'JOIN courts ct ON ct.id = r.court_id' : '',
        caseNumber !== undefined ? 'JOIN cases cs ON cs.id = r.case_id' : '',
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
    judges: async (row: Row, _: unknown, { pool }: Context) => {
      // Try the explicit case_judges link table first.
      const { rows } = await pool.query<Row>(
        `SELECT j.* FROM judges j
         JOIN case_judges cj ON cj.judge_id = j.id
         WHERE cj.case_id = $1`,
        [row.id],
      );
      if (rows.length > 0) return rows;

      // Fall back to judges referenced by the case's rulings.
      const { rows: fromRulings } = await pool.query<Row>(
        `SELECT DISTINCT j.* FROM judges j
         JOIN rulings r ON r.judge_id = j.id
         WHERE r.case_id = $1`,
        [row.id],
      );
      return fromRulings;
    },
    parties: async (row: Row, _: unknown, { pool }: Context) => {
      const { rows } = await pool.query<Row>(
        `SELECT p.* FROM parties p
         JOIN case_parties cp ON cp.party_id = p.id
         WHERE cp.case_id = $1`,
        [row.id],
      );
      return rows;
    },
  },

  Judge: {
    canonicalName: (row: Row) => row.canonical_name,
    isActive: (row: Row) => row.is_active,
    appointedAt: (row: Row) => row.appointed_at,
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
    postedAt: (row: Row) => row.posted_at,
    court: (row: Row, _: unknown, { loaders }: Context) =>
      row.court_id ? loaders.courtLoader.load(row.court_id as string) : null,
    judge: (row: Row, _: unknown, { loaders }: Context) =>
      row.judge_id ? loaders.judgeLoader.load(row.judge_id as string) : null,
    case: async (row: Row, _: unknown, { pool }: Context) => {
      if (!row.case_id) return null;
      const { rows } = await pool.query<Row>('SELECT * FROM cases WHERE id = $1', [row.case_id]);
      return rows[0] ?? null;
    },
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
    case: async (row: Row, _: unknown, { pool }: Context) => {
      if (!row.case_id) return null;
      const { rows } = await pool.query<Row>('SELECT * FROM cases WHERE id = $1', [row.case_id]);
      return rows[0] ?? null;
    },
  },

  Party: {
    canonicalName: (row: Row) => row.canonical_name,
    partyType: (row: Row) => row.party_type,
  },

  RulingSearchHit: {
    rulingId: (hit: Row) => hit.rulingId,
    caseNumber: (hit: Row) => hit.caseNumber,
    judgeName: (hit: Row) => hit.judgeName,
    hearingDate: (hit: Row) => hit.hearingDate,
  },

  // Auth resolvers
  ...authResolvers.User ? { User: authResolvers.User } : {},
  Mutation: authResolvers.Mutation,
};

// Merge auth Query resolvers into the main Query object
Object.assign(resolvers.Query, authResolvers.Query);
