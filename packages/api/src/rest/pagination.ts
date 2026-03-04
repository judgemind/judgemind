/**
 * Shared cursor-based pagination utilities for the REST API.
 * Cursors are opaque base64 strings encoding ordered column values
 * separated by "|" — consistent with the GraphQL resolver pattern.
 */

export function encodeCursor(parts: string[]): string {
  return Buffer.from(parts.join('|')).toString('base64');
}

export function decodeCursor(cursor: string): string[] {
  return Buffer.from(cursor, 'base64').toString('utf8').split('|');
}

/** Clamp page size to [1, 100], default 20. */
export function pageSize(limit: number | undefined | null): number {
  const n = limit ?? 20;
  return Math.min(Math.max(1, n), 100);
}

export interface PaginationMeta {
  has_more: boolean;
  next_cursor: string | null;
}

/**
 * Slice rows to `limit`, determine has_more, and compute the next cursor.
 * The caller fetches `limit + 1` rows to detect whether a next page exists.
 */
export function buildPage<T extends Record<string, unknown>>(
  rows: T[],
  limit: number,
  cursorFn: (row: T) => string[],
): { data: T[]; pagination: PaginationMeta } {
  const hasMore = rows.length > limit;
  const data = rows.slice(0, limit);
  const lastRow = data[data.length - 1];
  return {
    data,
    pagination: {
      has_more: hasMore,
      next_cursor: lastRow ? encodeCursor(cursorFn(lastRow)) : null,
    },
  };
}
