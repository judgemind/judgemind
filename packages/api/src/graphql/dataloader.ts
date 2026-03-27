import DataLoader from 'dataloader';
import type { Pool } from 'pg';

type Row = Record<string, unknown>;

/**
 * Group rows by a foreign-key column, returning an array of rows per key.
 * Keys that have no matching rows get an empty array.
 */
function groupByKey(rows: Row[], key: string, ids: readonly string[]): Row[][] {
  const map = new Map<string, Row[]>();
  for (const row of rows) {
    const k = row[key] as string;
    let arr = map.get(k);
    if (!arr) {
      arr = [];
      map.set(k, arr);
    }
    arr.push(row);
  }
  return ids.map((id) => map.get(id) ?? []);
}

/**
 * Create per-request DataLoaders for batching DB lookups.
 *
 * DataLoaders deduplicate and batch all calls that occur within a single
 * event-loop tick, replacing N individual SELECT queries with one
 * SELECT … WHERE id = ANY($1). Create a fresh set per request so that
 * stale data never leaks between requests.
 */
export function createLoaders(pool: Pool) {
  const courtLoader = new DataLoader<string, Row | null>(async (ids) => {
    const { rows } = await pool.query<Row>('SELECT * FROM courts WHERE id = ANY($1)', [ids]);
    const byId = new Map(rows.map((r) => [r.id as string, r]));
    return ids.map((id) => byId.get(id) ?? null);
  });

  const judgeLoader = new DataLoader<string, Row | null>(async (ids) => {
    const { rows } = await pool.query<Row>('SELECT * FROM judges WHERE id = ANY($1)', [ids]);
    const byId = new Map(rows.map((r) => [r.id as string, r]));
    return ids.map((id) => byId.get(id) ?? null);
  });

  /** Batch-load cases by ID. */
  const caseLoader = new DataLoader<string, Row | null>(async (ids) => {
    const { rows } = await pool.query<Row>('SELECT * FROM cases WHERE id = ANY($1)', [ids]);
    const byId = new Map(rows.map((r) => [r.id as string, r]));
    return ids.map((id) => byId.get(id) ?? null);
  });

  /** Batch-load document format by document ID. Returns only the format string. */
  const documentFormatLoader = new DataLoader<string, string | null>(async (ids) => {
    const { rows } = await pool.query<Row>(
      'SELECT id, format FROM documents WHERE id = ANY($1)',
      [ids],
    );
    const byId = new Map(rows.map((r) => [r.id as string, r.format as string]));
    return ids.map((id) => byId.get(id) ?? null);
  });

  /**
   * Batch-load judges for multiple cases. Returns an array of judge rows per
   * case_id. Uses the case_judges link table first; falls back to judges
   * referenced in the case's rulings if no explicit link exists.
   *
   * The fallback is handled per case within the batched result: if a case_id
   * has no rows in case_judges, we check the rulings table for that case.
   */
  const caseJudgesLoader = new DataLoader<string, Row[]>(async (caseIds) => {
    // Primary: explicit case_judges links
    const { rows: cjRows } = await pool.query<Row>(
      `SELECT j.*, cj.case_id
       FROM judges j
       JOIN case_judges cj ON cj.judge_id = j.id
       WHERE cj.case_id = ANY($1)`,
      [caseIds],
    );
    const grouped = groupByKey(cjRows, 'case_id', caseIds);

    // Find case IDs with no explicit links — need fallback via rulings
    const missingIds = caseIds.filter((_, i) => grouped[i].length === 0);
    if (missingIds.length > 0) {
      const { rows: fallbackRows } = await pool.query<Row>(
        `SELECT DISTINCT ON (r.case_id, j.id) j.*, r.case_id
         FROM judges j
         JOIN rulings r ON r.judge_id = j.id
         WHERE r.case_id = ANY($1)`,
        [missingIds],
      );
      const fallbackGrouped = groupByKey(fallbackRows, 'case_id', caseIds);
      for (let i = 0; i < caseIds.length; i++) {
        if (grouped[i].length === 0) {
          grouped[i] = fallbackGrouped[i];
        }
      }
    }

    // Remove the extra case_id column from judge rows
    for (const arr of grouped) {
      for (const row of arr) {
        delete row.case_id;
      }
    }

    return grouped;
  });

  /** Batch-load parties for multiple cases via the case_parties join table.
   *  Deduplicates by (canonical_name, role) to avoid showing the same party
   *  name multiple times when duplicate party records exist (#1426). */
  const casePartiesLoader = new DataLoader<string, Row[]>(async (caseIds) => {
    const { rows } = await pool.query<Row>(
      `SELECT DISTINCT ON (cp.case_id, LOWER(p.canonical_name), cp.role)
              p.*, cp.role, cp.case_id
       FROM parties p
       JOIN case_parties cp ON cp.party_id = p.id
       WHERE cp.case_id = ANY($1)
       ORDER BY cp.case_id, LOWER(p.canonical_name), cp.role, p.created_at`,
      [caseIds],
    );
    const grouped = groupByKey(rows, 'case_id', caseIds);
    // Remove the extra case_id column from party rows
    for (const arr of grouped) {
      for (const row of arr) {
        delete row.case_id;
      }
    }
    return grouped;
  });

  /** Batch-load the latest ruling for multiple cases.
   *  Returns the most recent ruling (by hearing_date DESC, id DESC) per case,
   *  excluding rulings with future hearing dates. */
  const latestRulingLoader = new DataLoader<string, Row | null>(async (caseIds) => {
    const { rows } = await pool.query<Row>(
      `SELECT DISTINCT ON (case_id) *, case_id AS _loader_case_id
       FROM rulings
       WHERE case_id = ANY($1)
         AND hearing_date <= CURRENT_DATE
       ORDER BY case_id, hearing_date DESC, id DESC`,
      [caseIds],
    );
    const byCase = new Map(rows.map((r) => {
      const cid = r._loader_case_id as string;
      delete r._loader_case_id;
      return [cid, r];
    }));
    return caseIds.map((id) => byCase.get(id) ?? null);
  });

  return {
    courtLoader,
    judgeLoader,
    caseLoader,
    documentFormatLoader,
    caseJudgesLoader,
    casePartiesLoader,
    latestRulingLoader,
  };
}

export type Loaders = ReturnType<typeof createLoaders>;
