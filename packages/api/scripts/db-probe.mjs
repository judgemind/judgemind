/**
 * DB reachability probe for `npm test` (scripts/run-tests.mjs). See #4711.
 *
 * The probe decides whether the pre-push `npm test` runs the integration
 * suite. It must connect the same way the tests will, so it honors the
 * connection's SSL mode instead of forcing SSL:
 *
 *   - `sslmode` in TEST_DATABASE_URL wins; else PGSSLMODE; else "prefer".
 *   - disable            -> plain TCP only.
 *   - require / verify-* -> SSL only, without cert verification (RDS tunnels
 *                           present self-signed certs).
 *   - prefer / allow / unset / unknown -> plain first, then SSL. A local
 *     docker-compose postgres (no SSL) takes the first branch; an
 *     SSL-only server takes the second.
 *
 * `sslmode` is stripped from the URL before probing: pg lets a URL sslmode
 * override the explicit `ssl` option, which would defeat the fallback.
 *
 * Kept free of side effects so tests/db-probe.unit.test.ts can import it.
 */

export const SSL_NO_VERIFY = Object.freeze({ rejectUnauthorized: false });

const SSL_ONLY_MODES = new Set(['require', 'verify-ca', 'verify-full', 'no-verify']);

/** Split `sslmode` out of a postgres URL. */
export function stripSslMode(url) {
  const qIndex = url.indexOf('?');
  if (qIndex === -1) return { connectionString: url, sslMode: null };
  const params = new URLSearchParams(url.slice(qIndex + 1));
  const raw = params.get('sslmode');
  if (raw === null) return { connectionString: url, sslMode: null };
  params.delete('sslmode');
  const rest = params.toString();
  const base = url.slice(0, qIndex);
  return { connectionString: rest ? `${base}?${rest}` : base, sslMode: raw.toLowerCase() };
}

/** Ordered list of pg `ssl` options to try for a given sslmode. */
export function sslCandidates(sslMode) {
  if (sslMode === 'disable') return [false];
  if (SSL_ONLY_MODES.has(sslMode)) return [SSL_NO_VERIFY];
  return [false, SSL_NO_VERIFY];
}

/**
 * Readable one-line reason. Node reports "localhost" connection refusals as
 * an AggregateError (one per resolved address) with an empty message.
 */
export function errorMessage(err) {
  if (!(err instanceof Error)) return String(err);
  if (err.message) return err.message;
  if (err instanceof AggregateError && err.errors.length > 0) {
    return err.errors.map(errorMessage).join('; ');
  }
  return err.code ?? err.name;
}

/**
 * Try each SSL candidate in order. Returns
 * `{ ok, ssl, attempts }` where `ssl` is the option that worked and
 * `attempts` lists the failures seen before (or instead of) success.
 */
export async function probeDatabase(url, { Pool, env = process.env, timeoutMs = 3000 }) {
  const { connectionString, sslMode } = stripSslMode(url);
  const mode = sslMode ?? env.PGSSLMODE?.toLowerCase() ?? null;
  const attempts = [];
  for (const ssl of sslCandidates(mode)) {
    const pool = new Pool({ connectionString, ssl, connectionTimeoutMillis: timeoutMs });
    try {
      await pool.query('SELECT 1');
      await pool.end().catch(() => {});
      return { ok: true, ssl, attempts };
    } catch (err) {
      attempts.push({ ssl, error: errorMessage(err) });
      await pool.end().catch(() => {});
    }
  }
  return { ok: false, ssl: null, attempts };
}

/**
 * Env for the vitest child process. The integration tests build their own
 * pg pools from TEST_DATABASE_URL; when that URL has no sslmode, pg reads
 * PGSSLMODE, so pin it to what the probe proved works.
 */
export function testEnvFor(url, ssl, baseEnv = process.env) {
  const env = { ...baseEnv };
  // Lets SSL test pools accept RDS-tunnel self-signed certs. No-op for
  // plain TCP (CI's postgres service container, local docker-compose).
  env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
  if (stripSslMode(url).sslMode === null) {
    env.PGSSLMODE = ssl ? 'no-verify' : 'disable';
  }
  return env;
}

function redact(url) {
  try {
    const u = new URL(url);
    if (u.password) u.password = '***';
    return u.toString();
  } catch {
    return url;
  }
}

const ENABLE_HELP = [
  'To run them, start the docker-compose postgres and opensearch (postgres',
  'creates judgemind_test; the tests apply migrations themselves), then:',
  '  docker compose up -d postgres opensearch',
  '  TEST_DATABASE_URL=postgresql://judgemind:localdev@localhost:5432/judgemind_test \\',
  '  TEST_OPENSEARCH_URL=http://localhost:9200 npm test',
  'See docs/agent/local-dev.md. CI always runs the full suite, so anything',
  'skipped here will still be tested there.',
];

/** Multi-line, hard-to-miss explanation of why integration tests were skipped. */
export function skipBanner({ reason, url, attempts = [] }) {
  const rule = '='.repeat(72);
  const lines = [rule, 'INTEGRATION TESTS SKIPPED — running unit tests only.', ''];
  if (reason === 'unset') {
    lines.push('Why: TEST_DATABASE_URL is not set.');
  } else {
    lines.push(`Why: could not connect to TEST_DATABASE_URL (${redact(url)}).`);
    for (const a of attempts) {
      lines.push(`  - ${a.ssl ? 'with SSL' : 'without SSL'}: ${a.error}`);
    }
  }
  lines.push('', ...ENABLE_HELP, rule);
  return lines.join('\n');
}
