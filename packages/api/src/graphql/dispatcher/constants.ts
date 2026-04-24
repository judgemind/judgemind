/**
 * Canonical terminal-status constants for the dispatcher agent state machine.
 *
 * Authoritative twin (Python): scripts/dispatcher/daemon.py::TERMINAL_AGENT_STATUSES
 *
 * A CI check (`scripts/check-dispatcher-terminal-statuses.sh`) asserts that
 * this list stays in sync with the Python frozenset. When adding a new
 * terminal status, update BOTH lists and the OUTCOME_STYLES map in
 * `packages/web/src/app/(main)/admin/dispatcher/ui-primitives.tsx`.
 */

export const TERMINAL_AGENT_STATUSES = [
  'succeeded',
  'failed',
  'crashed',
  'plan_blocked',
  'needs_review',
] as const;

export type TerminalAgentStatus = (typeof TERMINAL_AGENT_STATUSES)[number];
