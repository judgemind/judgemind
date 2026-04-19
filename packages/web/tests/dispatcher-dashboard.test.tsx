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
      spawnFrozenUntil: null,
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
          kind: 'task',
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
      spawnFrozenUntil: null,
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
    // Queue depth section
    expect(screen.getByText(/issues ready for pickup/i)).toBeInTheDocument();
    // "0" appears in both "Active agents (0)" heading and queue panel — the
    // heading carries the exact "Active agents (0)" text we want to assert.
    expect(screen.getByText(/Active agents \(0\)/)).toBeInTheDocument();
    // Config panel headings
    expect(screen.getByText('Concurrency cap')).toBeInTheDocument();
    expect(screen.getByText('Idle mode')).toBeInTheDocument();
    expect(screen.getByText('Backoff seconds')).toBeInTheDocument();
    expect(notFoundCallCount).toBe(0);
  });

  it('renders active agents with phase and issue number', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    // Agents table heading with count
    expect(screen.getByText(/Active agents \(1\)/)).toBeInTheDocument();
    // "#2731" appears both in the agents row and the recent-failures row.
    expect(screen.getAllByText('#2731').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/ralph-worker \(2\)/)).toBeInTheDocument();
    // Short agent id
    expect(screen.getByText(/agent-ac/i)).toBeInTheDocument();
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

  it('invokes dispatcherControl with "pause" when the Pause button is clicked', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    const pauseButton = screen.getByRole('button', { name: /Pause/i });
    fireEvent.click(pauseButton);
    expect(mockMutationFn).toHaveBeenCalledTimes(1);
    expect(mockMutationFn).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { command: 'pause', payload: {} },
      }),
    );
  });

  it('requires confirmation for destructive commands', () => {
    mockQueryResult.data = makeActiveState();
    mockQueryResult.loading = false;
    render(<DispatcherDashboard />);
    const drainButton = screen.getByRole('button', { name: /Stop \(drain\)/i });
    fireEvent.click(drainButton);
    // Mutation NOT yet invoked — waiting on confirm modal.
    expect(mockMutationFn).not.toHaveBeenCalled();
    // Confirm dialog should have appeared.
    const confirmButton = screen.getByTestId('confirm-dialog-confirm');
    fireEvent.click(confirmButton);
    expect(mockMutationFn).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { command: 'drain', payload: {} },
        context: expect.objectContaining({
          headers: expect.objectContaining({ 'X-MFA-Token': expect.any(String) }),
        }),
      }),
    );
  });

  it('renders an error banner when the query errors', () => {
    mockQueryResult.data = undefined;
    mockQueryResult.loading = false;
    mockQueryResult.error = new Error('network');
    render(<DispatcherDashboard />);
    expect(screen.getByText(/Failed to load dispatcher state/i)).toBeInTheDocument();
  });
});
