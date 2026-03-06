import type { Client } from '@opensearch-project/opensearch';
import type { Pool } from 'pg';

const INDEX_ALIAS = 'tentative_rulings';
const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 100;

export interface RulingFilters {
  court?: string;
  county?: string;
  state?: string;
  judgeName?: string;
  dateFrom?: string;
  dateTo?: string;
  caseNumber?: string;
}

export interface SearchRulingsArgs {
  query?: string;
  filters?: RulingFilters;
  first?: number;
  after?: string;
  includeFuture?: boolean;
}

export interface RulingSearchHit {
  rulingId: string;
  caseNumber: string | null;
  court: string | null;
  county: string | null;
  state: string | null;
  judgeName: string | null;
  hearingDate: string | null;
  excerpt: string | null;
  score: number | null;
}

interface SearchEdge {
  node: RulingSearchHit;
  cursor: string;
}

export interface SearchResult {
  edges: SearchEdge[];
  pageInfo: { hasNextPage: boolean; endCursor: string | null };
  totalHits: number;
}

function pageSize(first: number | undefined | null): number {
  const n = first ?? DEFAULT_PAGE_SIZE;
  return Math.min(Math.max(1, n), MAX_PAGE_SIZE);
}

function encodeCursor(sortValues: unknown[]): string {
  return Buffer.from(JSON.stringify(sortValues)).toString('base64');
}

function decodeCursor(cursor: string): unknown[] {
  return JSON.parse(Buffer.from(cursor, 'base64').toString('utf8')) as unknown[];
}

function buildQuery(
  query: string | undefined,
  filters: RulingFilters | undefined,
  includeFuture?: boolean,
) {
  const must: unknown[] = [];
  const filter: unknown[] = [];

  if (query) {
    must.push({ match: { ruling_text: { query, operator: 'and' } } });
  }

  if (filters) {
    if (filters.court) filter.push({ term: { court: filters.court } });
    if (filters.county) filter.push({ term: { county: filters.county } });
    if (filters.state) filter.push({ term: { state: filters.state } });
    if (filters.judgeName) filter.push({ term: { judge_name: filters.judgeName } });
    if (filters.caseNumber) filter.push({ prefix: { case_number: filters.caseNumber } });
  }

  // Build a single hearing_date range filter that merges the user's date
  // bounds with the default future-date exclusion (lte: now/d).
  {
    const range: Record<string, string> = {};
    if (filters?.dateFrom) range.gte = filters.dateFrom;
    if (filters?.dateTo) range.lte = filters.dateTo;
    if (!includeFuture && !range.lte) {
      range.lte = 'now/d';
    }
    if (Object.keys(range).length > 0) {
      filter.push({ range: { hearing_date: range } });
    }
  }

  // If no query and no filters (besides the future-date filter), match all
  if (must.length === 0 && filter.length === 0) {
    return { match_all: {} };
  }

  return {
    bool: {
      ...(must.length > 0 ? { must } : { must: [{ match_all: {} }] }),
      ...(filter.length > 0 ? { filter } : {}),
    },
  };
}

export async function searchRulings(
  os: Client,
  pool: Pool,
  args: SearchRulingsArgs,
): Promise<SearchResult> {
  const limit = pageSize(args.first);
  const osQuery = buildQuery(args.query, args.filters, args.includeFuture);

  // Sort: relevance first if query provided, then hearing_date desc, then _id for tiebreaker
  const sort: unknown[] = [];
  if (args.query) {
    sort.push({ _score: 'desc' });
  }
  sort.push({ hearing_date: { order: 'desc', missing: '_last' } });
  sort.push({ _id: 'desc' });

  const searchBody: Record<string, unknown> = {
    query: osQuery,
    sort,
    size: limit + 1, // fetch one extra to determine hasNextPage
    _source: ['case_number', 'court', 'county', 'state', 'judge_name', 'hearing_date', 'document_id'],
    track_total_hits: true,
  };

  if (args.query) {
    searchBody.highlight = {
      fields: { ruling_text: { fragment_size: 200, number_of_fragments: 1 } },
      pre_tags: ['<mark>'],
      post_tags: ['</mark>'],
    };
  }

  if (args.after) {
    searchBody.search_after = decodeCursor(args.after);
  }

  const response = await os.search({ index: INDEX_ALIAS, body: searchBody });

  const hits = response.body.hits.hits as Array<{
    _id: string;
    _score: number | null;
    _source: Record<string, unknown>;
    highlight?: { ruling_text?: string[] };
    sort: unknown[];
  }>;

  const totalHits =
    typeof response.body.hits.total === 'number'
      ? response.body.hits.total
      : (response.body.hits.total as { value: number }).value;

  const hasNextPage = hits.length > limit;
  const pageHits = hits.slice(0, limit);

  // Batch lookup ruling IDs from PG using document_ids
  const documentIds = pageHits.map((h) => h._source.document_id as string).filter(Boolean);
  const rulingIdMap = new Map<string, string>();

  if (documentIds.length > 0) {
    const { rows } = await pool.query<{ id: string; document_id: string }>(
      'SELECT id, document_id FROM rulings WHERE document_id = ANY($1)',
      [documentIds],
    );
    for (const row of rows) {
      rulingIdMap.set(row.document_id, row.id);
    }
  }

  const edges: SearchEdge[] = pageHits.map((hit) => {
    const src = hit._source;
    const docId = src.document_id as string;
    return {
      node: {
        rulingId: rulingIdMap.get(docId) ?? docId,
        caseNumber: (src.case_number as string) ?? null,
        court: (src.court as string) ?? null,
        county: (src.county as string) ?? null,
        state: (src.state as string) ?? null,
        judgeName: (src.judge_name as string) ?? null,
        hearingDate: (src.hearing_date as string) ?? null,
        excerpt: hit.highlight?.ruling_text?.[0] ?? null,
        score: hit._score ?? null,
      },
      cursor: encodeCursor(hit.sort),
    };
  });

  return {
    edges,
    pageInfo: {
      hasNextPage,
      endCursor: edges.length > 0 ? edges[edges.length - 1].cursor : null,
    },
    totalHits,
  };
}
