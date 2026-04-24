/**
 * Tests for the cockpit's expand-count full-list dialog (issue #3159).
 *
 * Coverage:
 *   - kind=null (closed) does NOT fire the underlying GraphQL query.
 *   - Empty list per kind renders the per-kind empty-state copy.
 *   - Small list (3 items) — every item rendered.
 *   - Large list (50 items) — every item rendered (no 10-cap).
 *   - Loading state shows the "Loading…" copy.
 *   - Error state shows the error message.
 *   - ESC / overlay click triggers `onClose` (the radix Dialog handles
 *     these natively; we exercise the path through `onOpenChange`).
 *   - `formatDialogTitle` pure-function unit tests.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Apollo mock — the dialog's `useQuery` returns one of three states:
//   - `skip: true` (kind=null): `{ data: undefined, loading: false }`.
//   - normal: returns the data we set on `mockData[kind]`, plus the
//     loading/error flags.
// We track invocation count so the kind=null assertion can verify the
// query was NOT fired.
// ---------------------------------------------------------------------------

const useQueryCalls: Array<{
  opName: string;
  variables?: { kind?: string };
  skip?: boolean;
}> = [];

let mockLoading = false;
let mockError: Error | undefined;

const mockData: Record<
  string,
  { dispatcherQueueFull?: Record<string, unknown> } | undefined
> = {
  READY: undefined,
  BLOCKED: undefined,
  COMPLETED: undefined,
};

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: (
      doc: { definitions?: Array<{ name?: { value?: string } }> },
      options?: {
        variables?: { kind?: string };
        skip?: boolean;
      },
    ) => {
      const opName = doc?.definitions?.[0]?.name?.value ?? '';
      useQueryCalls.push({
        opName,
        variables: options?.variables,
        skip: options?.skip,
      });
      if (options?.skip) {
        return {
          data: undefined,
          loading: false,
          error: undefined,
          refetch: vi.fn(),
        };
      }
      const kind = options?.variables?.kind ?? 'READY';
      return {
        data: mockData[kind],
        loading: mockLoading,
        error: mockError,
        refetch: vi.fn(),
      };
    },
  };
});

import {
  QueueFullDialog,
  formatDialogTitle,
} from '../QueueFullDialog';

function resetMocks() {
  useQueryCalls.length = 0;
  mockLoading = false;
  mockError = undefined;
  mockData.READY = undefined;
  mockData.BLOCKED = undefined;
  mockData.COMPLETED = undefined;
}

describe('QueueFullDialog — kind=null (closed)', () => {
  it('does NOT render dialog content when kind is null', () => {
    resetMocks();
    render(<QueueFullDialog kind={null} onClose={vi.fn()} />);
    // Closed Dialog: the content is unmounted via radix portal — the
    // body / title are not present in the DOM.
    expect(screen.queryByTestId('queue-full-dialog-body')).not.toBeInTheDocument();
    expect(screen.queryByTestId('queue-full-dialog-title')).not.toBeInTheDocument();
  });

  it('skips the `dispatcherQueueFull` query when kind is null', () => {
    resetMocks();
    render(<QueueFullDialog kind={null} onClose={vi.fn()} />);
    // The query was hooked, but with `skip: true` — we don't fetch.
    const calls = useQueryCalls.filter((c) => c.opName === 'DispatcherQueueFull');
    expect(calls.length).toBe(1);
    expect(calls[0].skip).toBe(true);
  });
});

describe('QueueFullDialog — empty list per kind', () => {
  it('READY: renders the agent-ready empty-state copy', () => {
    resetMocks();
    mockData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: [],
        completions: [],
      },
    };
    render(<QueueFullDialog kind="READY" onClose={vi.fn()} />);
    expect(screen.getByTestId('queue-full-dialog-empty')).toHaveTextContent(
      /Queue empty/i,
    );
  });

  it('BLOCKED: renders the blocked empty-state copy', () => {
    resetMocks();
    mockData.BLOCKED = {
      dispatcherQueueFull: {
        kind: 'BLOCKED',
        queueItems: [],
        completions: [],
      },
    };
    render(<QueueFullDialog kind="BLOCKED" onClose={vi.fn()} />);
    expect(screen.getByTestId('queue-full-dialog-empty')).toHaveTextContent(
      /no blocked issues/i,
    );
  });

  it('COMPLETED: renders the recently-completed empty-state copy', () => {
    resetMocks();
    mockData.COMPLETED = {
      dispatcherQueueFull: {
        kind: 'COMPLETED',
        queueItems: [],
        completions: [],
      },
    };
    render(<QueueFullDialog kind="COMPLETED" onClose={vi.fn()} />);
    expect(screen.getByTestId('queue-full-dialog-empty')).toHaveTextContent(
      /no completed agents yet/i,
    );
  });
});

describe('QueueFullDialog — small list', () => {
  it('READY: renders all 3 issue links', () => {
    resetMocks();
    mockData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: [
          {
            issueNumber: 100,
            title: 'first',
            priority: 'p1',
            labels: ['priority/p1'],
            createdAt: '2026-04-18T11:00:00Z',
            blockedBy: [],
            cooldownSecondsRemaining: null,
          },
          {
            issueNumber: 101,
            title: 'second',
            priority: 'p2',
            labels: ['priority/p2'],
            createdAt: '2026-04-18T11:00:00Z',
            blockedBy: [],
            cooldownSecondsRemaining: null,
          },
          {
            issueNumber: 102,
            title: 'third',
            priority: 'p3',
            labels: ['priority/p3'],
            createdAt: '2026-04-18T11:00:00Z',
            blockedBy: [],
            cooldownSecondsRemaining: null,
          },
        ],
        completions: [],
      },
    };
    render(<QueueFullDialog kind="READY" onClose={vi.fn()} />);
    expect(screen.getByTestId('queue-full-dialog-list')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-100')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-101')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-102')).toBeInTheDocument();
  });
});

describe('QueueFullDialog — large list (no 10-cap)', () => {
  it('READY: renders all 50 issues', () => {
    resetMocks();
    const items = Array.from({ length: 50 }, (_, i) => ({
      issueNumber: 1000 + i,
      title: `issue ${1000 + i}`,
      priority: 'p2',
      labels: ['priority/p2'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [],
      cooldownSecondsRemaining: null,
    }));
    mockData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: items,
        completions: [],
      },
    };
    render(<QueueFullDialog kind="READY" onClose={vi.fn()} />);
    // Spot-check the boundary entries (well past the panel's 10-cap).
    expect(screen.getByTestId('issue-link-1000')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-1010')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-1025')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-1049')).toBeInTheDocument();
  });

  it('COMPLETED: renders all 30 completions', () => {
    resetMocks();
    const completions = Array.from({ length: 30 }, (_, i) => ({
      agentId: `aaaaaaaa-bbbb-cccc-dddd-${String(i).padStart(12, '0')}`,
      issueNumber: 5000 + i,
      issueTitle: `done ${5000 + i}`,
      priority: 'p2',
      status: 'succeeded',
      endedAt: '2026-04-18T11:55:00Z',
      prNumber: 6000 + i,
      totalTokens: null,
      totalCostUsd: null,
      failureSummary: null,
      mergedAt: '2026-04-18T11:50:00Z',
      verifiedAt: '2026-04-18T11:53:00Z',
      verifySkipReason: null,
      retroedAt: '2026-04-18T11:55:00Z',
    }));
    mockData.COMPLETED = {
      dispatcherQueueFull: {
        kind: 'COMPLETED',
        queueItems: [],
        completions,
      },
    };
    render(<QueueFullDialog kind="COMPLETED" onClose={vi.fn()} />);
    // Each completion row is keyed by `completion-row-<agentId>`.
    expect(
      screen.getByTestId(
        'completion-row-aaaaaaaa-bbbb-cccc-dddd-000000000000',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(
        'completion-row-aaaaaaaa-bbbb-cccc-dddd-000000000029',
      ),
    ).toBeInTheDocument();
  });
});

describe('QueueFullDialog — loading + error states', () => {
  it('shows the loading copy when the query is in flight', () => {
    resetMocks();
    mockLoading = true;
    render(<QueueFullDialog kind="READY" onClose={vi.fn()} />);
    expect(screen.getByTestId('queue-full-dialog-loading')).toHaveTextContent(
      /Loading/i,
    );
  });

  it('shows the error copy when the query fails', () => {
    resetMocks();
    mockError = new Error('Network down');
    render(<QueueFullDialog kind="READY" onClose={vi.fn()} />);
    expect(screen.getByTestId('queue-full-dialog-error')).toHaveTextContent(
      /Network down/i,
    );
  });
});

describe('QueueFullDialog — close handling', () => {
  it('triggers onClose when the dialog dismisses (ESC / overlay click)', () => {
    resetMocks();
    mockData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: [],
        completions: [],
      },
    };
    const onClose = vi.fn();
    render(<QueueFullDialog kind="READY" onClose={onClose} />);
    // Simulate ESC by firing keyDown on the dialog content.
    fireEvent.keyDown(screen.getByTestId('queue-full-dialog'), {
      key: 'Escape',
      code: 'Escape',
    });
    expect(onClose).toHaveBeenCalled();
  });
});

describe('formatDialogTitle', () => {
  it('returns empty string for kind=null', () => {
    expect(formatDialogTitle(null, 0, false)).toBe('');
  });

  it('renders Agent-ready queue (N) for READY', () => {
    expect(formatDialogTitle('READY', 12, false)).toBe(
      'Agent-ready queue (12)',
    );
  });

  it('renders Blocked queue (N) for BLOCKED', () => {
    expect(formatDialogTitle('BLOCKED', 5, false)).toBe('Blocked queue (5)');
  });

  it('renders Recently completed (N) for COMPLETED', () => {
    expect(formatDialogTitle('COMPLETED', 7, false)).toBe(
      'Recently completed (7)',
    );
  });

  it('appends a (loading…) marker while loading', () => {
    expect(formatDialogTitle('READY', 0, true)).toBe(
      'Agent-ready queue (loading…)',
    );
  });
});
