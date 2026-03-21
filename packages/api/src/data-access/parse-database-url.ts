/**
 * Parse a PostgreSQL connection URL, extracting and stripping `sslmode` so
 * that SSL can be configured via the `pg` driver's `ssl` option instead.
 *
 * The pg driver (v8.x) maps sslmode=require to verify-full, which can fail
 * against RDS depending on the Node.js base image CA bundle. This utility
 * strips the sslmode parameter from the URL and returns an `ssl` option
 * using `rejectUnauthorized: false` — matching standard libpq sslmode=require
 * semantics (encrypt without certificate verification).
 */

export interface ParsedDatabaseUrl {
  /** Connection string with `sslmode` parameter removed (if it was present). */
  connectionString: string;
  /** SSL configuration for the `pg` Pool/Client constructor. */
  ssl: false | { rejectUnauthorized: false };
}

export function parseDatabaseUrl(url: string): ParsedDatabaseUrl {
  const needsSsl = /[?&]sslmode=/.test(url);
  if (!needsSsl) {
    return { connectionString: url, ssl: false };
  }

  // Use URLSearchParams for robust query string manipulation, handling
  // sslmode in any position (first, middle, last, or only parameter).
  const qIndex = url.indexOf('?');
  const baseUrl = qIndex === -1 ? url : url.slice(0, qIndex);
  const queryString = qIndex === -1 ? '' : url.slice(qIndex + 1);

  const params = new URLSearchParams(queryString);
  params.delete('sslmode');
  const remaining = params.toString();

  const connectionString = remaining ? `${baseUrl}?${remaining}` : baseUrl;

  return {
    connectionString,
    ssl: { rejectUnauthorized: false },
  };
}
