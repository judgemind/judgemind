/**
 * Pure helpers for extracting structured fields from GitHub issue
 * labels and bodies. **No fetch, no network, no secrets** — these
 * functions take already-loaded data and return derived values.
 *
 * Issue #2820 split these out of the (now-deleted) dispatcher-admin
 * GitHub REST client so the resolvers can keep using them after the
 * fetch path went away. Do **not** re-add any GitHub API calls here;
 * the operator constraint is that the API container must not have the
 * ability to burn the shared PAT budget. Absence of the credential
 * (``GITHUB_TOKEN`` has been removed from the API task-def — see
 * ``infra/terraform/modules/api-service/main.tf``) makes absence of
 * the fetch code load-bearing. If a future feature needs GitHub data,
 * the daemon is the only place that should be fetching it, persisting
 * the result to ``dispatcher.*`` for the API to read.
 */

/**
 * Extract `Blocked by #N` numbers from an issue body. Matches the
 * same pattern the `unblock-issues` workflow uses.
 *
 * Returns an empty array for null/empty/undefined input — callers can
 * unconditionally spread the result into a response object without a
 * null guard.
 */
export function parseBlockedBy(body: string | null | undefined): number[] {
  if (!body) return [];
  const matches = body.matchAll(/Blocked by #(\d+)/g);
  const numbers: number[] = [];
  for (const match of matches) {
    const n = Number.parseInt(match[1], 10);
    if (Number.isFinite(n)) numbers.push(n);
  }
  return numbers;
}

/**
 * Extract a priority label (`p0`/`p1`/`p2`/`p3`) from a label list.
 * Returns `null` when no `priority/pN` label is present.
 */
export function extractPriority(
  labels: readonly string[] | null | undefined,
): string | null {
  if (!labels) return null;
  for (const label of labels) {
    const match = label.match(/^priority\/(p[0-3])$/);
    if (match) return match[1];
  }
  return null;
}
