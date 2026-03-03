import DataLoader from 'dataloader';
import type { Pool } from 'pg';

type Row = Record<string, unknown>;

/**
 * Create per-request DataLoaders for batching court and judge lookups.
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

  return { courtLoader, judgeLoader };
}

export type Loaders = ReturnType<typeof createLoaders>;
