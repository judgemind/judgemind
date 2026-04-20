/**
 * Canonical dispatcher phase names and the forward flow string for the
 * admin cockpit's phase tooltip (#2899).
 *
 * This is a TS mirror of `scripts/dispatcher/phases.py`. A parity test
 * in `__tests__/dispatcher-phase-flow.test.ts` reads the Python source
 * and asserts the two lists stay in sync — when a phase is added or
 * removed, update both files in the same PR. CI catches drift.
 *
 * Why two copies instead of a GraphQL field?
 * ------------------------------------------
 * Phase names almost never change. Plumbing them through the
 * GraphQL schema would add a server round-trip on every admin-page
 * render for data that would be identical 99.9% of the time; the
 * parity test gives us the "single source of truth" guarantee at
 * build time at zero runtime cost.
 *
 * ASCII-only
 * ----------
 * The rendered string is injected into the native `title` attribute
 * of the phase chip, which does not parse HTML. Unicode arrow
 * (U+2192) is safe because `title` renders the Unicode code point
 * directly. No markup.
 */

/**
 * Happy-path phase sequence in daemon execution order. Matches
 * `docs/specs/dispatcher-v2-spec.md` §6 and the per-agent state
 * machine in `scripts/dispatcher/daemon.py`. Every phase listed is
 * something the `ActiveAgents` row can display as its current
 * `phase` value; the mechanical phases (claiming, setup,
 * push_and_pr, ci_watch, merge, deploy_watch) are included so
 * operators see the full pipeline.
 */
export const PHASE_FLOW_FORWARD: readonly string[] = [
  'claiming',
  'planning',
  'setup',
  'ralph',
  'summary',
  'push_and_pr',
  'ci_watch',
  'merge',
  'deploy_watch',
  'verify',
  'retro',
] as const;

/**
 * Phases that only appear on the unhappy branch. The tooltip
 * decorates these with a trailing `*` to signal they are conditional.
 */
export const CONDITIONAL_PHASES: ReadonlySet<string> = new Set(['fix_ci']);

/**
 * Canonical-form match for the admin tooltip's current-phase
 * highlighting. The daemon sometimes writes phase names with hyphens
 * (legacy alias — `fix-ci` vs `fix_ci`), so both sides normalize
 * before comparison. The conditional marker `fix_ci*` also matches
 * `fix_ci`.
 */
function matchesCurrent(flowEntry: string, currentPhase: string): boolean {
  const canonicalEntry = flowEntry.replace(/\*$/, '').replace(/-/g, '_');
  const canonicalCurrent = currentPhase.replace(/-/g, '_');
  return canonicalEntry === canonicalCurrent;
}

/**
 * Render the forward flow as an arrow-separated string. When
 * `currentPhase` is provided and matches a phase in the flow, that
 * phase is surrounded by square brackets so the operator's eye can
 * land on their current position.
 *
 * The single conditional marker `fix_ci*` is inserted between
 * `ci_watch` and `merge` so the tooltip communicates that the red-CI
 * branch exists without forcing the operator to read a diagram.
 */
export function buildFlowString(currentPhase: string | null = null): string {
  const assembled: string[] = [];
  for (const name of PHASE_FLOW_FORWARD) {
    assembled.push(name);
    if (name === 'ci_watch') {
      // fix_ci* lives on the red-CI branch between ci_watch and merge.
      assembled.push('fix_ci*');
    }
  }
  const decorated = currentPhase
    ? assembled.map((name) =>
        matchesCurrent(name, currentPhase) ? `[${name}]` : name,
      )
    : assembled;
  return decorated.join(' \u2192 ');
}

/**
 * Two-line admin-tooltip text for a given phase. Matches the format
 * in issue #2899:
 *
 *     Currently in <phase> phase.
 *     Flow: <phase1> \u2192 <phase2> \u2192 ...
 */
export function tooltipText(currentPhase: string): string {
  const flow = buildFlowString(currentPhase);
  return `Currently in ${currentPhase} phase.\nFlow: ${flow}`;
}
