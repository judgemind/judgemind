import { Pool, types } from 'pg';
import { parseDatabaseUrl } from './parse-database-url';

// Return DATE, TIMESTAMP, and TIMESTAMPTZ columns as ISO strings rather than
// JavaScript Date objects. This avoids timezone-shift surprises and makes the
// values safe to pass directly to GraphQL String fields.
types.setTypeParser(1082, (val: string) => val); // DATE       → 'YYYY-MM-DD'
types.setTypeParser(1114, (val: string) => val); // TIMESTAMP  → 'YYYY-MM-DD HH:MI:SS'
types.setTypeParser(1184, (val: string) => val); // TIMESTAMPTZ → ISO string

const rawUrl =
  process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind';

const { connectionString, ssl } = parseDatabaseUrl(rawUrl);

export const pool = new Pool({
  connectionString,
  ssl,
  max: 10,
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 5_000,
});
