import { Pool, types } from 'pg';

// Return DATE, TIMESTAMP, and TIMESTAMPTZ columns as ISO strings rather than
// JavaScript Date objects. This avoids timezone-shift surprises and makes the
// values safe to pass directly to GraphQL String fields.
types.setTypeParser(1082, (val: string) => val); // DATE       → 'YYYY-MM-DD'
types.setTypeParser(1114, (val: string) => val); // TIMESTAMP  → 'YYYY-MM-DD HH:MI:SS'
types.setTypeParser(1184, (val: string) => val); // TIMESTAMPTZ → ISO string

const connectionString =
  process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind';

// When connecting to RDS with sslmode=require, the pg driver (v8.x) maps it to
// verify-full, which requires the server certificate to match the hostname.
// Depending on the Node.js base image and CA bundle this can fail silently.
// Explicitly set rejectUnauthorized: false for sslmode=require to match standard
// libpq semantics (encrypt the connection without verifying the certificate).
const needsSsl = connectionString.includes('sslmode=');
const sslOpts = needsSsl ? { rejectUnauthorized: false } : false;

export const pool = new Pool({
  connectionString,
  ssl: sslOpts,
  max: 10,
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 5_000,
});
