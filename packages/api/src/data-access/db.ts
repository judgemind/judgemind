import { Pool, types } from 'pg';

// Return DATE, TIMESTAMP, and TIMESTAMPTZ columns as ISO strings rather than
// JavaScript Date objects. This avoids timezone-shift surprises and makes the
// values safe to pass directly to GraphQL String fields.
types.setTypeParser(1082, (val: string) => val); // DATE       → 'YYYY-MM-DD'
types.setTypeParser(1114, (val: string) => val); // TIMESTAMP  → 'YYYY-MM-DD HH:MI:SS'
types.setTypeParser(1184, (val: string) => val); // TIMESTAMPTZ → ISO string

const rawUrl =
  process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind';

// The pg driver (v8.x) maps sslmode=require to verify-full, which can fail
// against RDS depending on the Node.js base image CA bundle. Strip the sslmode
// parameter from the URL and configure SSL via the Pool's ssl option instead,
// using rejectUnauthorized: false (encrypt without certificate verification —
// matching standard libpq sslmode=require semantics).
const needsSsl = /[?&]sslmode=/.test(rawUrl);
const connectionString = needsSsl
  ? rawUrl.replace(/[?&]sslmode=[^&]*/g, '').replace(/\?$/, '')
  : rawUrl;

export const pool = new Pool({
  connectionString,
  ssl: needsSsl ? { rejectUnauthorized: false } : false,
  max: 10,
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 5_000,
});
