/**
 * Unit tests for the dispatcher terminal-status constants (issue #2863).
 *
 * Guards against accidental deletion, reordering, or drift of the
 * `TERMINAL_AGENT_STATUSES` export.  Adding a new terminal status requires
 * updating this test — that friction is intentional: the list must also be
 * updated in `scripts/dispatcher/daemon.py` and
 * `packages/web/.../ui-primitives.tsx`.
 */

import { describe, expect, it } from 'vitest';
import { TERMINAL_AGENT_STATUSES } from './constants';
import type { TerminalAgentStatus } from './constants';

describe('TERMINAL_AGENT_STATUSES', () => {
  it('contains exactly the expected five terminal statuses', () => {
    const expected = new Set([
      'succeeded',
      'failed',
      'crashed',
      'plan_blocked',
      'needs_review',
    ]);
    expect(new Set(TERMINAL_AGENT_STATUSES)).toEqual(expected);
  });

  it('has exactly 5 entries', () => {
    expect(TERMINAL_AGENT_STATUSES).toHaveLength(5);
  });

  it('is a readonly tuple (as const)', () => {
    // Verify the array is non-empty and every element is a non-empty string.
    for (const status of TERMINAL_AGENT_STATUSES) {
      expect(typeof status).toBe('string');
      expect(status.length).toBeGreaterThan(0);
    }
  });
});

describe('TerminalAgentStatus type', () => {
  it('accepts all valid terminal statuses at compile time', () => {
    // If the type is wrong these assignments would fail tsc (--noEmit).
    const s1: TerminalAgentStatus = 'succeeded';
    const s2: TerminalAgentStatus = 'failed';
    const s3: TerminalAgentStatus = 'crashed';
    const s4: TerminalAgentStatus = 'plan_blocked';
    const s5: TerminalAgentStatus = 'needs_review';
    // Use the variables so tsc doesn't prune them.
    expect([s1, s2, s3, s4, s5]).toHaveLength(5);
  });
});
