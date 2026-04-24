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

/**
 * Map of `priority/pN` labels to their numeric sort rank. Lower is
 * more urgent. Mirrors ``_PRIORITY_LABEL_RANKS`` in
 * ``scripts/dispatcher/daemon.py`` so the admin-page queue display
 * matches the daemon's pick order (issue #2843 / parallel to #2835).
 */
const PRIORITY_LABEL_RANKS: Readonly<Record<string, number>> = {
  'priority/p0': 0,
  'priority/p1': 1,
  'priority/p2': 2,
  'priority/p3': 3,
};

/**
 * Rank assigned to issues that carry no recognised `priority/pN`
 * label. Sinks below p3 so priority-labelled work always wins a
 * head-to-head against unlabelled work, but unlabelled issues remain
 * eligible once the labelled ones are exhausted.
 */
export const PRIORITY_RANK_NO_LABEL = 4;

/**
 * Return the lowest (most-urgent) priority rank implied by `labels`.
 *
 * - `priority/p0` → 0
 * - `priority/p1` → 1
 * - `priority/p2` → 2
 * - `priority/p3` → 3
 * - No priority label (or empty / malformed input) →
 *   {@link PRIORITY_RANK_NO_LABEL} (4)
 *
 * Used as the primary sort key for the admin page's queue display so
 * the operator sees issues in the same order the daemon picks them
 * (parallel to `_priority_rank` in `scripts/dispatcher/daemon.py`
 * from issue #2835).
 *
 * When an issue carries more than one priority label (shouldn't
 * happen, but defensive), the lowest rank wins — picking the most
 * urgent interpretation matches operator intent.
 *
 * Tolerates non-array input (returns the no-label floor) and ignores
 * non-string entries, so a malformed ``issues_json`` row cannot crash
 * the sort.
 *
 * Cross-language contract: scripts/dispatcher/tests/fixtures/priority_rank_cases.json is the source of truth; Python mirror is _priority_rank in scripts/dispatcher/daemon.py.
 */
export function priorityRank(labels: unknown): number {
  if (!Array.isArray(labels)) return PRIORITY_RANK_NO_LABEL;
  let best = PRIORITY_RANK_NO_LABEL;
  for (const entry of labels) {
    if (typeof entry !== 'string') continue;
    const rank = PRIORITY_LABEL_RANKS[entry];
    if (rank !== undefined && rank < best) {
      best = rank;
    }
  }
  return best;
}
