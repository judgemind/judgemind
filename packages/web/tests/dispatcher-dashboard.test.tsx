import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks — declared before imports
// ---------------------------------------------------------------------------

let mockAuthResult: {
  user: {
    id: string;
    email: string;
    emailVerified: boolean;
    displayName: string | null;
    role: string;
    createdAt: string;
  } | null;
  loading: boolean;
  setUser: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
};

let mockQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
  refetch: ReturnType<typeof vi.fn>;
};

let mockMutationFn: ReturnType<typeof vi.fn>;
let mockMutationLoading: boolean;
let notFoundCallCount = 0;

vi.mock('@/providers/AuthProvider', () => ({
  useAuth: () => mockAuthResult,
}));

vi.mock('next/navigation', () => ({
  notFound: () => {
    notFoundCallCount += 1;
    // Throw to mimic Next.js behavior (aborts render).
    throw new Error('NEXT_NOT_FOUND');
  },
}));

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>('@apollo/client');
  return {
    ...actual,
    useQuery: () => mockQueryResult,
    useMutation: () => [mockMutationFn, { loading: mockMutationLoading }],
  };
});

// ---------------------------------------------------------------------------
// Import after mocks
// ---------------------------------------------------------------------------

import { DispatcherDashboard } from '../src/app/(main)/admin/dispatcher/DispatcherDashboard';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeAdminUser() {
  return {
    id: 'user-1',
    email: 'admin@example.com',
    emailVerified: true,
    displayName: 'Admin',
    role: 'admin',
    createdAt: '2026-01-01T00:00:00Z',
  };
}

function makeEmptyState() {
  return {
    dispatcherState: {
      currentRun: null,
      activeAgents: [],
      recentFailures: [],
      queueDepth: 0,
      blockedDepth: 0,
      queueReady: [],
      queueBlocked: [],
      recentCompletions: [],
      config: [
        {
          key: 'concurrency_cap',
          value: '3',
          updatedAt: '2026-04-18T00:00:00Z',
          updatedBy: 'init',
        },
      ],
      spawnFrozenUntil: null,
      circuitBreakerOpen: false,
      capFlippedBy: null,
    },
  };
}

function makeActiveState() {
  return {
    dispatcherState: {
      currentRun: {
        runId: 'run-1',
        startedAt: '2026-04-18T11:59:00Z',
        stoppedAt: null,
        heartbeatTs: '2026-04-18T11:59:45Z',
        versionSha: 'abcdef12345678',
        host: 'dispatcher-prod-1',
        pid: 1234,
      },
      activeAgents: [
        {
          id: 'agent-aca547d3-long-id',
          issueNumber: 2731,
          worktreePath: '.claude/worktrees/agent-aca547d3',
          phase: 'ralph-worker (2)',
          status: 'running',
          startedAt: '2026-04-18T11:30:00Z',
          endedAt: null,
          exitCode: null,
          prNumber: null,
          retriesUsed: 0,
        },
      ],
      recentFailures: [
        {
          failureId: 'f-1',
          agentId: 'agent-aca547d3-long-id',
          category: 'autocompact_loop',
          detectedBy: 'supervisor',
          details: {},
          ts: '2026-04-18T11:45:00Z',
          issueNumber: 2731,
        },
      ],
      queueDepth: 5,
      blockedDepth: 0,
      queueReady: [],
      queueBlocked: [],
      recentCompletions: [],
      config: [
        {
          key: 'concurrency_cap',
          value: '3',
          updatedAt: '2026-04-18T00:00:00Z',
          updatedBy: 'init',
        },
      ],
      spawnFrozenUntil: null,
      circuitBreakerOpen: false,
      capFlippedBy: null,
    },
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DispatcherDashboard', () => {
  beforeEach(() => {
    notFoundCallCount = 0;
    mockAuthResult = {
      user: makeAdminUser(),
      loading: false,
      setUser: vi.fn(),
      logout: vi.fn(),
    };
    mockQueryResult = {
      data: undefined,
      loading: true,
      error: undefined,
      refetch: vi.fn().mockResolvedValue({ data: undefined }),
    };
    mockMutationFn = vi.fn().mockResolvedValue({ data: undefined });
    mockMutationLoading = false;
  });

  it('renders a skeleton while auth is loading', () => {
    mockAuthResult.loading = true;
    mockAuthResult.user = null;
    const { container } = render(<DispatcherDashboard />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
    expect(notFoundCallCount).toBe(0);
  });

  it('calls notFound for unauthenticated users', () => {
    mockAuthResult.user = null;
    expect(() => render(<DispatcherDashboard />)).toThrowError('NEXT_NOT_FOUND');
    // React may retry rendering a throwing component; assert the mock was
    // invoked at least once rather than pinning the exact count.
    expect(notFoundCallCount).toBeGreaterThanOrEqual(1);
  });

  it('calls notFound for non-admin users', () => {
    mockAuthResult.user = { ...makeAdminUser(), role: 'user' };
    expect(() => render(<DispatcherDashboard />)).toThrowError('NEXT_NOT_FOUND');
    expect(notFoundCallCount).toBeGreaterThanOrEqual(1);
  });

  it('renders the empty shell with "stopped" status and empty states', () => {
    mockQueryResult.data = makeEmptyState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    expect(screen.getByRole('heading', { level: 1, name: /Dispatcher/i })).toBeInTheDocument();
    // Status pill
    const pill = screen.getByTestId('daemon-status-pill');
    expect(pill).toHaveTextContent(/stopped/i);
    // Active agents empty state
    expect(screen.getByText(/No active agents\./i)).toBeInTheDocument();
    // Recent failures empty state
    expect(screen.getByText(/No failures in the last 24 hours\./i)).toBeInTheDocument();
    // Queue empty state (split ready/blocked panels per #2805 §1.3)
    expect(screen.getByText(/Queue empty/i)).toBeInTheDocument();
    expect(screen.getByText(/No blocked issues\./i)).toBeInTheDocument();
    // Recently-completed panel empty state (#2805 §1.5)
    expect(screen.getByText(/No completed agents yet/i)).toBeInTheDocument();
    // Active agents heading with zero count.
    expect(screen.getByText(/Active agents \(0\)/)).toBeInTheDocument();
    // Config strip shows the inline cap button (editable via click).
    expect(screen.getByTestId('config-value-concurrency_cap')).toBeInTheDocument();
    expect(notFoundCallCount).toBe(0);
  });

  it('renders active agents with phase and issue number', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    // Agents row heading with count
    expect(screen.getByText(/Active agents \(1\)/)).toBeInTheDocument();
    // Issue number is a hot link via IssueLink (#2805 §1.4).
    // The same issue can appear in multiple panels (active + failures),
    // so there may be more than one matching element.
    const issueLinks = screen.getAllByTestId('issue-link-2731');
    expect(issueLinks.length).toBeGreaterThanOrEqual(1);
    expect(issueLinks[0]).toHaveAttribute('target', '_blank');
    expect(screen.getByText(/ralph-worker \(2\)/)).toBeInTheDocument();
    // Short agent id truncated to 8 chars
    expect(screen.getByText(/agent-ac…/)).toBeInTheDocument();
  });

  it('renders a "running" status pill for a healthy run', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    // Stub Date.now so the heartbeat appears fresh.
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-04-18T12:00:00Z'));
    try {
      render(<DispatcherDashboard />);
      const pill = screen.getByTestId('daemon-status-pill');
      expect(pill).toHaveTextContent(/running/i);
    } finally {
      nowSpy.mockRestore();
    }
  });

  it('invokes dispatcherControl with "stop" when the Stop button is clicked (#2884 graceful)', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    // #2884: Stop is graceful — no confirm modal, fires immediately.
    // This is the whole point of the simplification: stopping dev
    // work should not be friction-heavy.
    const stopButton = screen.getByRole('button', { name: /^stop$/i });
    fireEvent.click(stopButton);
    expect(mockMutationFn).toHaveBeenCalledTimes(1);
    expect(mockMutationFn).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { command: 'stop', payload: {} },
      }),
    );
    // #2884: no X-MFA-Token header — the MFA re-auth placeholder was
    // removed. Admin session auth is the only gate.
    const call = mockMutationFn.mock.calls[0][0];
    expect(call.context).toBeUndefined();
  });

  it('requires confirmation for Force Stop (immediate abort, destructive)', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    const forceStopButton = screen.getByRole('button', { name: /force stop/i });
    fireEvent.click(forceStopButton);
    // Mutation NOT yet invoked — waiting on confirm modal.
    expect(mockMutationFn).not.toHaveBeenCalled();
    // Confirm dialog should have appeared.
    const confirmButton = screen.getByTestId('confirm-dialog-confirm');
    fireEvent.click(confirmButton);
    expect(mockMutationFn).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { command: 'force_stop', payload: {} },
      }),
    );
    // #2884: no X-MFA-Token header — even for the destructive
    // variant. Admin session auth is sufficient; the confirm modal
    // provides the "are you sure" friction.
    const call = mockMutationFn.mock.calls[0][0];
    expect(call.context).toBeUndefined();
  });

  it('renders exactly three control buttons (#2884): Start, Stop, Force Stop', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    const controls = screen.getByTestId('dispatcher-controls');
    const buttons = controls.querySelectorAll('button');
    expect(buttons.length).toBe(3);
    expect(buttons[0]).toHaveTextContent(/^start$/i);
    expect(buttons[1]).toHaveTextContent(/^stop$/i);
    expect(buttons[2]).toHaveTextContent(/^force stop$/i);
    // Pause/Resume/Drain must be gone.
    expect(screen.queryByRole('button', { name: /^pause$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^resume$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /stop \(drain\)/i })).toBeNull();
  });

  it('renders an error banner when the query errors', () => {
    mockQueryResult.data = undefined;
    mockQueryResult.loading = false;
    mockQueryResult.error = new Error('network');
    render(<DispatcherDashboard />);
    expect(screen.getByText(/Failed to load dispatcher state/i)).toBeInTheDocument();
    // Stale indicator is only shown when we have cached state — on
    // initial-load failure the full error banner takes over instead
    // (#2842).
    expect(screen.queryByTestId('dispatcher-stale-indicator')).not.toBeInTheDocument();
  });

  it('keeps rendering the dashboard with a stale indicator when a polling refetch fails (#2842)', () => {
    // Simulate Apollo's polling contract: `data` from the last successful
    // fetch stays populated while `error` reflects the latest failure.
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    mockQueryResult.error = new Error('network blip');
    render(<DispatcherDashboard />);
    // Dashboard is still rendered — no ErrorBanner takeover.
    expect(screen.getByRole('heading', { level: 1, name: /Dispatcher/i })).toBeInTheDocument();
    expect(screen.queryByText(/Failed to load dispatcher state/i)).not.toBeInTheDocument();
    // Cached content is still visible (active agents row from makeActiveState).
    expect(screen.getByText(/Active agents \(1\)/)).toBeInTheDocument();
    // Stale indicator is visible so the operator knows the latest poll failed.
    expect(screen.getByTestId('dispatcher-stale-indicator')).toBeInTheDocument();
  });

  it('clears the stale indicator when the next poll succeeds (#2842)', () => {
    // First render: transient poll failure after a successful load.
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    mockQueryResult.error = new Error('network blip');
    const { rerender } = render(<DispatcherDashboard />);
    expect(screen.getByTestId('dispatcher-stale-indicator')).toBeInTheDocument();
    // Second render: the next poll succeeds, so `error` clears while
    // `data` stays populated.
    mockQueryResult.error = undefined;
    rerender(<DispatcherDashboard />);
    expect(screen.queryByTestId('dispatcher-stale-indicator')).not.toBeInTheDocument();
    // Dashboard is still rendered and there was no retry click.
    expect(screen.getByRole('heading', { level: 1, name: /Dispatcher/i })).toBeInTheDocument();
  });
});
