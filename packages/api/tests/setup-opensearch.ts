/**
 * Shared OpenSearch setup for integration tests.
 *
 * TEST OPENSEARCH ISOLATION (mirrors #3006 DB URL pattern)
 * ---------------------------------------------------------
 * Integration-test OpenSearch setup reads ONLY `TEST_OPENSEARCH_URL`, never
 * the operational `OPENSEARCH_URL`. This prevents test tooling that happens
 * to run in an environment with `OPENSEARCH_URL` set (e.g. a ralph subagent
 * inside the dispatcher Fargate container, which exposes dev OpenSearch in
 * its env) from accidentally targeting a shared/operational OpenSearch cluster.
 *
 * A hostname allowlist inside `getTestOpenSearchUrl()` provides a second
 * layer of defense: even if `TEST_OPENSEARCH_URL` is accidentally set to a
 * non-local endpoint, the call is refused. See `OPENSEARCH_ALLOWED_HOSTS`
 * below.
 */

/**
 * Hostnames that are safe to use as an OpenSearch endpoint during tests.
 * Any other hostname is implicitly rejected. Extend only when adding a new
 * test-only OpenSearch location (e.g. a Testcontainers alias).
 *
 * Kept exported so the unit test in `setup-opensearch.unit.test.ts` can
 * assert on the current set without hardcoding it in two places.
 */
export const OPENSEARCH_ALLOWED_HOSTS = new Set(['localhost', '127.0.0.1', 'opensearch']);

/**
 * Return the OpenSearch URL for integration tests.
 *
 * Reads `TEST_OPENSEARCH_URL` and validates it against the allowlist.
 * Throws with a clear message if the var is unset, malformed, or points
 * to a non-allowlisted host.
 */
export function getTestOpenSearchUrl(): string {
  const testOsUrl = process.env.TEST_OPENSEARCH_URL;

  if (!testOsUrl) {
    throw new Error(
      'TEST_OPENSEARCH_URL is not set; integration tests require an explicit ' +
        'test OpenSearch URL. Set TEST_OPENSEARCH_URL to a local/test OpenSearch ' +
        '(e.g. http://localhost:9200) ' +
        'or run unit-only tests via `npm run test:unit`.'
    );
  }

  let parsed: URL;
  try {
    parsed = new URL(testOsUrl);
  } catch {
    throw new Error(
      `getTestOpenSearchUrl refused: TEST_OPENSEARCH_URL is not a valid URL: ` +
        `'${testOsUrl}'. Expected an OpenSearch connection URL.`
    );
  }

  if (!OPENSEARCH_ALLOWED_HOSTS.has(parsed.hostname)) {
    throw new Error(
      `getTestOpenSearchUrl refused: TEST_OPENSEARCH_URL host '${parsed.hostname}' ` +
        `is not in the test-OpenSearch allowlist. Tests must never target a ` +
        `shared cluster. Allowlist: ${[...OPENSEARCH_ALLOWED_HOSTS].join(', ')}`
    );
  }

  return testOsUrl;
}
