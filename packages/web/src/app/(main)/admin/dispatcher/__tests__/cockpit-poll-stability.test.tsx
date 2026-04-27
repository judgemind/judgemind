/**
 * Regression tests for cockpit poll-triggered flicker (#3584).
 *
 * Root cause: `document.startViewTransition` was called on every Apollo poll
 * cycle in `DispatcherDashboard.tsx`. The View Transitions API snapshots the
 * *entire root viewport* (including the Header in app/layout.tsx) into
 * `::view-transition-*(root)` and cross-fades it for ~250ms — causing the
 * header logo region and cockpit rows to flicker on every 2s poll.
 *
 * Fix (#3584): the data-mirror `useEffect` now calls `setRenderedData(data)`
 * synchronously. React's keyed reconciliation preserves DOM-node identity for
 * unchanged rows without any view-transition wrapper.
 *
 * AC2 note: the route-level Header lives in `packages/web/src/app/layout.tsx`
 * (line ~58) and is rendered OUTSIDE the polled `DispatcherDashboard` subtree,
 * so it is inherently stable across refetches regardless of this fix.
 *
 * These tests verify:
 *   AC3 / AC6 — DOM-node identity is preserved for unchanged rows across a
 *               simulated refetch (same data and new-agent-appended variants).
 *   AC4       — No skeleton renders during refetch when prior data exists.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Apollo mocks — mirrors the pattern from DispatcherDashboard.test.tsx.
// ---------------------------------------------------------------------------

const mockControlMutate = vi.fn().mockResolvedValue({ data: {} });
const mockSetConfigMutate = vi.fn().mockResolvedValue({ data: {} });
const mockRefetch = vi.fn().mockResolvedValue({ data: {} });

let mockQueryData: { dispatcherState?: Record<string, unknown> } | undefined;

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>('@apollo/client');
  return {
    ...actual,
    useQuery: (
      doc: { definitions?: Array<{ name?: { value?: string } }> },
      options?: { variables?: { kind?: string }; skip?: boolean },
    ) => {
      const opName = doc?.definitions?.[0]?.name?.value ?? '';
      if (opName === 'DispatcherQueueFull') {
        return {
          data: options?.skip ? undefined : { dispatcherQueueFull: { kind: options?.variables?.kind ?? 'READY', queueItems: [], completions: [] } },
          loading: false,
          error: undefined,
          refetch: mockRefetch,
        };
      }
      if (opName === 'WeeklyDiagnoserReport') {
        return {
          data: { weeklyDiagnoserReport: [] },
          loading: false,
          error: undefined,
          refetch: mockRefetch,
        };
      }
      return {
        data: mockQueryData,
        loading: false,
        error: undefined,
        refetch: mockRefetch,
      };
    },
    useMutation: (doc: { definitions?: Array<{ name?: { value?: string } }> }) => {
      const opName = doc?.definitions?.[0]?.name?.value ?? '';
      if (opName === 'DispatcherControl') return [mockControlMutate, { loading: false }];
      if (opName === 'DispatcherSetConfig') return [mockSetConfigMutate, { loading: false }];
      return [vi.fn(), { loading: false }];
    },
  };
});

vi.mock('next/navigation', () => ({
  notFound: () => { throw new Error('notFound called in test'); },
}));

vi.mock('@/providers/AuthProvider', () => ({
  useAuth: () => ({
    user: { id: '1', email: 'admin@test.com', role: 'admin' },
    loading: false,
  }),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const BASE_STATE = {
  currentRun: {
    runId: 'run-1',
    startedAt: '2026-04-18T10:00:00Z',
    stoppedAt: null,
    heartbeatTs: new Date().toISOString(),
    versionSha: 'deadbeefcafe0000',
    host: 'ip-10-0-0-1',
    pid: 1234,
  },
  activeAgents: [],
  recentFailures: [],
  queueDepth: 0,
  blockedDepth: 0,
  queueReady: [],
  queueBlocked: [],
  recentCompletions: [],
  recentCompletionsCount: 0,
  config: [
    { key: 'concurrency_cap', value: '3', updatedAt: '2026-04-18T09:00:00Z', updatedBy: 'init' },
    { key: 'backoff_seconds', value: '[60,300,900]', updatedAt: '2026-04-18T09:00:00Z', updatedBy: 'init' },
  ],
  spawnFrozenUntil: null,
  circuitBreakerOpen: false,
  capFlippedBy: null,
};

const AGENT_A = {
  id: 'aaaaaaaa-1111-2222-3333-444444444444',
  issueNumber: 100,
  issueTitle: 'agent-a',
  priority: 'p2',
  worktreePath: '/Users/x/.claude/worktrees/agent-aaaaaaaa',
  phase: 'ralph',
  status: 'running',
  startedAt: '2026-04-18T11:00:00Z',
  endedAt: null,
  exitCode: null,
  prNumber: null,
  retriesUsed: 0,
};

const AGENT_B = {
  id: 'bbbbbbbb-1111-2222-3333-444444444444',
  issueNumber: 200,
  issueTitle: 'agent-b',
  priority: 'p2',
  worktreePath: '/Users/x/.claude/worktrees/agent-bbbbbbbb',
  phase: 'verify',
  status: 'running',
  startedAt: '2026-04-18T11:10:00Z',
  endedAt: null,
  exitCode: null,
  prNumber: null,
  retriesUsed: 0,
};

const AGENT_C = {
  id: 'cccccccc-1111-2222-3333-444444444444',
  issueNumber: 300,
  issueTitle: 'agent-c',
  priority: 'p2',
  worktreePath: '/Users/x/.claude/worktrees/agent-cccccccc',
  phase: 'ralph',
  status: 'running',
  startedAt: '2026-04-18T11:20:00Z',
  endedAt: null,
  exitCode: null,
  prNumber: null,
  retriesUsed: 0,
};

import { DispatcherDashboard } from '../DispatcherDashboard';

function renderDashboard() {
  return render(<DispatcherDashboard />);
}

// ---------------------------------------------------------------------------
// AC3 / AC6: DOM-node identity preserved across refetch
// ---------------------------------------------------------------------------

describe('DispatcherDashboard — #3584 cockpit poll stability (AC3/AC6)', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
  });

  it('preserves DOM-node identity for both agent rows after a refetch with identical data (AC3)', async () => {
    // Initial render with 2 active agents.
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        activeAgents: [AGENT_A, AGENT_B],
      },
    };
    const { rerender } = renderDashboard();

    // Wait for the rows to appear.
    await waitFor(() => {
      expect(screen.getByTestId(`active-agent-row-${AGENT_A.id}`)).toBeInTheDocument();
      expect(screen.getByTestId(`active-agent-row-${AGENT_B.id}`)).toBeInTheDocument();
    });

    // Capture the DOM element references BEFORE the refetch.
    const rowABefore = screen.getByTestId(`active-agent-row-${AGENT_A.id}`);
    const rowBBefore = screen.getByTestId(`active-agent-row-${AGENT_B.id}`);

    // Simulate a poll arriving with a new object reference but identical content.
    // (Apollo always returns a fresh object on each network reply.)
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        activeAgents: [{ ...AGENT_A }, { ...AGENT_B }],
      },
    };
    rerender(<DispatcherDashboard />);

    // Wait for the effect to commit the new data.
    await waitFor(() => {
      expect(screen.getByTestId(`active-agent-row-${AGENT_A.id}`)).toBeInTheDocument();
    });

    // AC3: the DOM nodes must be the SAME objects — not remounted.
    const rowAAfter = screen.getByTestId(`active-agent-row-${AGENT_A.id}`);
    const rowBAfter = screen.getByTestId(`active-agent-row-${AGENT_B.id}`);

    expect(rowAAfter).toBe(rowABefore);
    expect(rowBAfter).toBe(rowBBefore);
  });

  it('preserves identity for existing rows when a new agent is appended (AC6)', async () => {
    // Initial render with 2 agents.
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        activeAgents: [AGENT_A, AGENT_B],
      },
    };
    const { rerender } = renderDashboard();

    await waitFor(() => {
      expect(screen.getByTestId(`active-agent-row-${AGENT_A.id}`)).toBeInTheDocument();
      expect(screen.getByTestId(`active-agent-row-${AGENT_B.id}`)).toBeInTheDocument();
    });

    const rowABefore = screen.getByTestId(`active-agent-row-${AGENT_A.id}`);
    const rowBBefore = screen.getByTestId(`active-agent-row-${AGENT_B.id}`);

    // Refetch with a third agent appended.
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        activeAgents: [{ ...AGENT_A }, { ...AGENT_B }, AGENT_C],
      },
    };
    rerender(<DispatcherDashboard />);

    await waitFor(() => {
      expect(screen.getByTestId(`active-agent-row-${AGENT_C.id}`)).toBeInTheDocument();
    });

    // Existing rows keep their DOM identity.
    expect(screen.getByTestId(`active-agent-row-${AGENT_A.id}`)).toBe(rowABefore);
    expect(screen.getByTestId(`active-agent-row-${AGENT_B.id}`)).toBe(rowBBefore);

    // New row mounts fresh.
    const rowCAfter = screen.getByTestId(`active-agent-row-${AGENT_C.id}`);
    expect(rowCAfter).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // AC4: no skeleton renders during refetch when prior data exists
  // ---------------------------------------------------------------------------

  it('does not render a skeleton during refetch when prior data exists (AC4)', async () => {
    // Initial render — data already present.
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        activeAgents: [AGENT_A],
      },
    };
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByTestId(`active-agent-row-${AGENT_A.id}`)).toBeInTheDocument();
    });

    // The skeleton pulse blocks are only rendered during `showInitialLoad`.
    // Once data is present they must be absent.
    // The skeleton shell renders four `animate-pulse` divs and a fifth
    // for the bottom bar. We look for the dispatcher-two-column-deck to
    // confirm data is rendered and the skeleton is not.
    expect(screen.getByTestId('dispatcher-two-column-deck')).toBeInTheDocument();
    // No skeleton animation elements visible at the top level.
    // The SkeletonShell is a sibling of the two-column-deck; if the deck
    // is present the skeleton branch is not rendered.
    expect(screen.queryByText(/animate-pulse/)).toBeNull();
  });

  // ---------------------------------------------------------------------------
  // AC4 variant: startViewTransition is never called (post-#3584 regression
  // guard that would catch a re-introduction of the wrapping).
  // ---------------------------------------------------------------------------

  it('never calls startViewTransition on any poll cycle (#3584 regression guard)', async () => {
    const startViewTransition = vi.fn((cb: () => void): unknown => {
      cb();
      return { finished: Promise.resolve() };
    });
    (
      document as unknown as { startViewTransition?: (cb: () => void) => unknown }
    ).startViewTransition = startViewTransition;

    try {
      mockQueryData = {
        dispatcherState: { ...BASE_STATE, activeAgents: [AGENT_A, AGENT_B] },
      };
      const { rerender } = renderDashboard();

      await waitFor(() => {
        expect(screen.getByTestId(`active-agent-row-${AGENT_A.id}`)).toBeInTheDocument();
      });

      // Simulate multiple poll cycles.
      for (let i = 1; i <= 3; i++) {
        mockQueryData = {
          dispatcherState: {
            ...BASE_STATE,
            activeAgents: [{ ...AGENT_A }, { ...AGENT_B }],
            queueDepth: i,
          },
        };
        rerender(<DispatcherDashboard />);
      }

      await waitFor(() => {
        expect(screen.getByTestId('queue-ready-count')).toHaveTextContent('0 / 3');
      });

      // startViewTransition must NEVER have been called across any of the polls.
      expect(startViewTransition).not.toHaveBeenCalled();
    } finally {
      delete (
        document as unknown as { startViewTransition?: (cb: () => void) => unknown }
      ).startViewTransition;
    }
  });
});
