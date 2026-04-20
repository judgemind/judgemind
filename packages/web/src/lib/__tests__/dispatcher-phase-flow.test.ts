/**
 * Tests for the dispatcher phase-flow tooltip helpers (#2899) plus a
 * parity check against `scripts/dispatcher/phases.py`.
 *
 * Why the parity check
 * --------------------
 * The admin cockpit's phase tooltip reads from the TS constant in
 * `../dispatcher-phase-flow.ts`, but the canonical sequence lives in
 * `scripts/dispatcher/phases.py` (the daemon is the source of truth
 * for what phases the state machine actually advances through). Without
 * a parity check the two could drift — e.g. a future PR adds a new
 * phase to the daemon's Python side but forgets the TS mirror, and
 * operators see an incomplete flow string in the tooltip even though
 * the agent is in the new phase.
 *
 * The parity check reads the Python source file as text, extracts the
 * `PHASE_FLOW_FORWARD` tuple via a simple line-based parse (no Python
 * interpreter needed — the JS test runner already has file I/O), and
 * asserts the TS `PHASE_FLOW_FORWARD` array matches element-for-element.
 * A mismatch fails CI with a diff-friendly error message.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  CONDITIONAL_PHASES,
  PHASE_FLOW_FORWARD,
  buildFlowString,
  tooltipText,
} from '../dispatcher-phase-flow';

// ---------------------------------------------------------------------------
// Parity test — TS constant MUST match the Python source
// ---------------------------------------------------------------------------

/**
 * Parse `PHASE_FLOW_FORWARD` out of the Python source by reading until
 * the closing `)` after the tuple opens. We do NOT try to be a full
 * Python parser — the file is our own, and we control its format. If
 * the shape changes (say, someone rewrites it as a list), update this
 * regex alongside the source.
 */
function extractPythonPhaseFlow(pythonSource: string): string[] {
  // Find the assignment, not the docstring mention. The tuple literal
  // always starts with `PHASE_FLOW_FORWARD: tuple[str, ...] = (`.
  const assignMatch = pythonSource.match(
    /^PHASE_FLOW_FORWARD\s*:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\(/m,
  );
  if (!assignMatch || assignMatch.index === undefined) {
    throw new Error(
      'phases.py is missing the `PHASE_FLOW_FORWARD: tuple[str, ...] = (` ' +
        'assignment — the parity test parser expects that exact shape.',
    );
  }
  const openParen =
    assignMatch.index + assignMatch[0].length - 1; // position of the `(`
  const closeParen = pythonSource.indexOf(')', openParen);
  if (closeParen === -1) {
    throw new Error(
      'phases.py PHASE_FLOW_FORWARD tuple has no closing `)` — ' +
        'the parity test parser expects a simple tuple, not a multi-line ' +
        'computed value.',
    );
  }
  const tupleBody = pythonSource.slice(openParen + 1, closeParen);
  // Match each "quoted string" entry — Python uses either "..." or
  // '...'. Keep regex tolerant of whitespace and trailing commas.
  const matches = tupleBody.match(/["']([a-zA-Z0-9_]+)["']/g);
  if (!matches) return [];
  return matches.map((m) => m.slice(1, -1));
}

describe('dispatcher-phase-flow parity with scripts/dispatcher/phases.py', () => {
  it('TS PHASE_FLOW_FORWARD matches the Python PHASE_FLOW_FORWARD', () => {
    // Resolve repo root by climbing from this test file. Four levels:
    // __tests__ → lib → src → packages/web → repo root.
    const repoRoot = join(__dirname, '..', '..', '..', '..', '..');
    const pythonPath = join(repoRoot, 'scripts', 'dispatcher', 'phases.py');
    const pythonSource = readFileSync(pythonPath, 'utf8');

    const pythonFlow = extractPythonPhaseFlow(pythonSource);
    const tsFlow = [...PHASE_FLOW_FORWARD];

    expect(tsFlow).toEqual(pythonFlow);
  });
});

// ---------------------------------------------------------------------------
// buildFlowString — decoration + conditional marker
// ---------------------------------------------------------------------------

describe('buildFlowString', () => {
  it('renders every forward phase separated by a Unicode arrow', () => {
    const flow = buildFlowString();
    for (const phase of PHASE_FLOW_FORWARD) {
      expect(flow).toContain(phase);
    }
    expect(flow).toContain('\u2192'); // U+2192 RIGHTWARDS ARROW
  });

  it('inserts the conditional fix_ci* marker between ci_watch and merge', () => {
    const flow = buildFlowString();
    const ciWatchIdx = flow.indexOf('ci_watch');
    const fixCiIdx = flow.indexOf('fix_ci*');
    const mergeIdx = flow.indexOf('merge');
    expect(ciWatchIdx).toBeGreaterThan(-1);
    expect(fixCiIdx).toBeGreaterThan(ciWatchIdx);
    expect(mergeIdx).toBeGreaterThan(fixCiIdx);
  });

  it('brackets the current phase so the operator can spot their position', () => {
    const flow = buildFlowString('ralph');
    expect(flow).toContain('[ralph]');
    // Other phases stay unbracketed.
    expect(flow).toMatch(/(^|[^[])planning([^\]]|$)/);
  });

  it('brackets the current phase even when the input uses hyphens (fix-ci)', () => {
    const flow = buildFlowString('fix-ci');
    expect(flow).toContain('[fix_ci*]');
  });

  it('leaves the output unbracketed when the current phase is unknown', () => {
    const flow = buildFlowString('bogus_phase');
    expect(flow).not.toContain('[');
  });

  it('exposes fix_ci in CONDITIONAL_PHASES so future phases can mirror the pattern', () => {
    expect(CONDITIONAL_PHASES.has('fix_ci')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// tooltipText — two-line format
// ---------------------------------------------------------------------------

describe('tooltipText', () => {
  it('renders the two-line "Currently in <phase>" + "Flow: ..." format', () => {
    const text = tooltipText('ralph');
    const lines = text.split('\n');
    expect(lines).toHaveLength(2);
    expect(lines[0]).toBe('Currently in ralph phase.');
    expect(lines[1]).toMatch(/^Flow: /);
  });

  it('preserves the full flow string including the conditional fix_ci* marker', () => {
    const text = tooltipText('planning');
    expect(text).toContain('fix_ci*');
    expect(text).toContain('[planning]');
  });
});
