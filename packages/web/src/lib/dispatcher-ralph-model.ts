/**
 * Ralph model-selection constants and helpers for the admin cockpit (#3021).
 *
 * This is a TS mirror of the relevant constants in
 * `scripts/dispatcher/daemon.py`. A parity test in
 * `__tests__/dispatcher-ralph-model.test.ts` reads the Python source
 * and asserts the two values stay in sync — when the cap changes,
 * update both files in the same PR. CI catches drift.
 *
 * Why two copies instead of a GraphQL field?
 * ------------------------------------------
 * `MAX_RETRY_ATTEMPTS` almost never changes. Plumbing it through the
 * GraphQL schema would add a server round-trip on every admin-page
 * render for data that would be identical 99.9% of the time; the
 * parity test gives us the "single source of truth" guarantee at
 * build time at zero runtime cost.
 *
 * Same design rationale as `dispatcher-phase-flow.ts`.
 */

/**
 * Hard cap on auto-retry attempts per agent+reason. Mirrors
 * `MAX_RETRY_ATTEMPTS` in `scripts/dispatcher/daemon.py:1024`.
 *
 * On the final attempt (attempt N = MAX_RETRY_ATTEMPTS) the daemon
 * escalates ralph from Sonnet to Opus (#2955). This constant drives
 * the cockpit's final-attempt Opus marker without a server round-trip.
 */
export const MAX_RETRY_ATTEMPTS = 3;

/**
 * Return the model name for a given ralph attempt number, mirroring
 * `_ralph_model_for_attempt` in `scripts/dispatcher/daemon.py:562-587`.
 *
 * `attemptN` is 1-indexed: the first run is attempt 1 and the final
 * budgeted retry is {@link MAX_RETRY_ATTEMPTS}. On the final attempt
 * the model upgrades from `'sonnet'` to `'opus'`. Defensive: attempts
 * numerically past {@link MAX_RETRY_ATTEMPTS} stay on Opus rather than
 * silently downgrading.
 */
export function ralphModelForAttempt(attemptN: number): 'sonnet' | 'opus' {
  if (attemptN >= MAX_RETRY_ATTEMPTS) {
    return 'opus';
  }
  return 'sonnet';
}

/**
 * Convenience predicate: returns `true` when the agent is on its final
 * budgeted ralph attempt, meaning `retriesUsed + 1 >= MAX_RETRY_ATTEMPTS`.
 *
 * `retriesUsed` is the 0-based count of retries already consumed
 * (`DispatcherAgent.retriesUsed`). Adding 1 gives the 1-indexed current
 * attempt number, which is then compared to {@link MAX_RETRY_ATTEMPTS}.
 */
export function isFinalRalphAttempt(retriesUsed: number): boolean {
  return retriesUsed + 1 >= MAX_RETRY_ATTEMPTS;
}
