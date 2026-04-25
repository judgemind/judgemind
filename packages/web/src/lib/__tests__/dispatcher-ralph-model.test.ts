/**
 * Tests for the dispatcher ralph-model helpers (#3021) plus a parity
 * check against `scripts/dispatcher/daemon.py`.
 *
 * Why the parity check
 * --------------------
 * The admin cockpit's final-attempt Opus marker reads
 * `MAX_RETRY_ATTEMPTS` from the TS constant in
 * `../dispatcher-ralph-model.ts`, but the canonical value lives in
 * `scripts/dispatcher/daemon.py` (the daemon is the source of truth
 * for retry budget). Without a parity check the two could drift — e.g.
 * a future PR bumps `MAX_RETRY_ATTEMPTS` in the daemon but forgets the
 * TS mirror, and the cockpit marks the wrong attempt as final.
 *
 * The parity check reads the Python source file as text, extracts the
 * `MAX_RETRY_ATTEMPTS = N` line via a simple regex (no Python interpreter
 * needed — the JS test runner already has file I/O), and asserts numeric
 * equality. A mismatch fails CI with a descriptive error message.
 *
 * Same pattern as `dispatcher-phase-flow.test.ts`.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  MAX_RETRY_ATTEMPTS,
  isFinalRalphAttempt,
  ralphModelForAttempt,
} from '../dispatcher-ralph-model';

// ---------------------------------------------------------------------------
// Parity test — TS constant MUST match the Python source
// ---------------------------------------------------------------------------

describe('dispatcher-ralph-model parity with scripts/dispatcher/daemon.py', () => {
  it('TS MAX_RETRY_ATTEMPTS matches the Python MAX_RETRY_ATTEMPTS', () => {
    // Resolve repo root by climbing from this test file. Four levels:
    // __tests__ → lib → src → packages/web → repo root.
    const repoRoot = join(__dirname, '..', '..', '..', '..', '..');
    const daemonPath = join(repoRoot, 'scripts', 'dispatcher', 'daemon.py');
    const daemonSource = readFileSync(daemonPath, 'utf8');

    const match = daemonSource.match(/^MAX_RETRY_ATTEMPTS\s*=\s*(\d+)/m);
    if (!match) {
      throw new Error(
        'daemon.py is missing the `MAX_RETRY_ATTEMPTS = N` assignment — ' +
          'the parity test parser expects that exact shape.',
      );
    }
    const pythonValue = parseInt(match[1], 10);
    expect(MAX_RETRY_ATTEMPTS).toBe(pythonValue);
  });
});

// ---------------------------------------------------------------------------
// ralphModelForAttempt — attempt-number to model name
// ---------------------------------------------------------------------------

describe('ralphModelForAttempt', () => {
  it('returns sonnet for attempts 1 through MAX_RETRY_ATTEMPTS-1', () => {
    for (let attempt = 1; attempt < MAX_RETRY_ATTEMPTS; attempt++) {
      expect(ralphModelForAttempt(attempt)).toBe('sonnet');
    }
  });

  it('returns opus for the final attempt (MAX_RETRY_ATTEMPTS)', () => {
    expect(ralphModelForAttempt(MAX_RETRY_ATTEMPTS)).toBe('opus');
  });

  it('returns opus defensively for attempts past the cap', () => {
    expect(ralphModelForAttempt(MAX_RETRY_ATTEMPTS + 1)).toBe('opus');
  });
});

// ---------------------------------------------------------------------------
// isFinalRalphAttempt — truth table over retriesUsed
// ---------------------------------------------------------------------------

describe('isFinalRalphAttempt', () => {
  it('returns false when retriesUsed=0 (first attempt, not final)', () => {
    expect(isFinalRalphAttempt(0)).toBe(false);
  });

  it('returns false when retriesUsed=1 (second attempt, not yet final)', () => {
    expect(isFinalRalphAttempt(1)).toBe(false);
  });

  it('returns true when retriesUsed=2 (third attempt = MAX_RETRY_ATTEMPTS)', () => {
    // retriesUsed=2 means attempt_n = 3 = MAX_RETRY_ATTEMPTS → final
    expect(isFinalRalphAttempt(2)).toBe(true);
  });

  it('returns true when retriesUsed=3 (past cap — defensive)', () => {
    expect(isFinalRalphAttempt(3)).toBe(true);
  });
});
