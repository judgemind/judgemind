/**
 * Tests for the dispatcher admin dashboard (#2805).
 *
 * Focus areas:
 *   - #2884: the three-button control surface — Start, Stop, Force
 *     Stop. Stop is graceful (no confirm modal, fires immediately);
 *     Force Stop pops the ConfirmDialog.
 *   - #2884: control and config-edit mutations do NOT inject any
 *     `X-MFA-Token` header — the MFA re-auth placeholder was removed.
 *   - Lowering `concurrency_cap` pops the ConfirmDialog before committing.
 *   - Raising `concurrency_cap` commits immediately without dialog.
 *   - State-flow two-column layout (#2823): Active → Queue-ready (left),
 *     Recently-completed → Queue-blocked (right). Mobile collapses to
 *     a single column in the same order.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Apollo mocks — one global `useMutation` that distinguishes by operation
// name, so the test can inspect the `context.headers` sent on each call.
// ---------------------------------------------------------------------------

const mockControlMutate = vi.fn().mockResolvedValue({ data: {} });
const mockSetConfigMutate = vi.fn().mockResolvedValue({ data: {} });
const mockRefetch = vi.fn().mockResolvedValue({ data: {} });
// #4063: per-query refetch trackers. The polled `DispatcherState` query
// and the once-on-mount `DispatcherConfig` query each get their own
// refetch fn so tests can assert which one a given side-effect targets.
const mockStateRefetch = vi.fn().mockResolvedValue({ data: {} });
const mockConfigRefetch = vi.fn().mockResolvedValue({ data: {} });
// #4063: track useQuery options per operation name so tests can
// assert `DispatcherState` is polled and `DispatcherConfig` is not.
const useQueryCalls: Record<
  string,
  Array<{ pollInterval?: number; skip?: boolean }>
> = {};

let mockQueryData: { dispatcherState?: Record<string, unknown> } | undefined;
// #4063: mock for the new config-only query. Defaults to mirroring the
// `BASE_STATE.config` fixture so tests that don't care about the split
// keep working.
let mockConfigData:
  | { dispatcherState?: { config?: Record<string, unknown>[] } }
  | undefined;
// #3159: separate mock data for the `dispatcherQueueFull` query fired
// only when the operator opens an expand-count dialog. Keyed by the
// `kind` variable so the mock can serve READY / BLOCKED / COMPLETED
// distinctly. Default empty so existing tests don't accidentally render
// dialog rows.
const mockQueueFullData: Record<
  string,
  { dispatcherQueueFull?: Record<string, unknown> } | undefined
> = {
  READY: undefined,
  BLOCKED: undefined,
  COMPLETED: undefined,
};

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>('@apollo/client');
  return {
    ...actual,
    useQuery: (
      doc: { definitions?: Array<{ name?: { value?: string } }> },
      options?: {
        variables?: { kind?: string };
        skip?: boolean;
        pollInterval?: number;
      },
    ) => {
      const opName = doc?.definitions?.[0]?.name?.value ?? '';
      // #4063: record every useQuery invocation so tests can assert the
      // poll-interval contract per operation.
      (useQueryCalls[opName] ??= []).push({
        pollInterval: options?.pollInterval,
        skip: options?.skip,
      });
      if (opName === 'DispatcherQueueFull') {
        // #3159: serve per-kind mock data. When `skip` is true (dialog
        // closed) Apollo would return `data: undefined`; mirror that
        // semantic here so tests that don't open the dialog don't see
        // stray dialog rows.
        if (options?.skip) {
          return {
            data: undefined,
            loading: false,
            error: undefined,
            refetch: mockRefetch,
          };
        }
        const kind = options?.variables?.kind ?? 'READY';
        return {
          data: mockQueueFullData[kind],
          loading: false,
          error: undefined,
          refetch: mockRefetch,
        };
      }
      if (opName === 'WeeklyDiagnoserReport') {
        // #2800: return an empty array so existing tests don't fail on
        // the new panel; the panel renders the empty-state copy quietly.
        return {
          data: { weeklyDiagnoserReport: [] },
          loading: false,
          error: undefined,
          refetch: mockRefetch,
        };
      }
      if (opName === 'DispatcherConfig') {
        // #4063: standalone config query — fires once on mount, not in
        // the 2s poll. Default to mirroring `mockQueryData.dispatcherState.config`
        // so tests written against the old combined query don't drift.
        const fallback = mockQueryData?.dispatcherState?.config as
          | Record<string, unknown>[]
          | undefined;
        const data =
          mockConfigData ??
          (fallback ? { dispatcherState: { config: fallback } } : undefined);
        return {
          data,
          loading: false,
          error: undefined,
          refetch: mockConfigRefetch,
        };
      }
      return {
        data: mockQueryData,
        loading: false,
        error: undefined,
        refetch: mockStateRefetch,
      };
    },
    useMutation: (doc: { definitions?: Array<{ name?: { value?: string } }> }) => {
      const opName = doc?.definitions?.[0]?.name?.value ?? '';
      if (opName === 'DispatcherControl') {
        return [mockControlMutate, { loading: false }];
      }
      if (opName === 'DispatcherSetConfig') {
        return [mockSetConfigMutate, { loading: false }];
      }
      return [vi.fn(), { loading: false }];
    },
  };
});

vi.mock('next/navigation', () => ({
  notFound: () => {
    throw new Error('notFound called in test');
  },
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
    {
      key: 'concurrency_cap',
      value: '3',
      updatedAt: '2026-04-18T09:00:00Z',
      updatedBy: 'init',
    },
    {
      key: 'backoff_seconds',
      value: '[60,300,900]',
      updatedAt: '2026-04-18T09:00:00Z',
      updatedBy: 'init',
    },
  ],
  spawnFrozenUntil: null,
  circuitBreakerOpen: false,
  capFlippedBy: null,
};

import { DispatcherDashboard } from '../DispatcherDashboard';

function renderDashboard() {
  return render(<DispatcherDashboard />);
}

describe('DispatcherDashboard — #2884 simplified command surface', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('Start fires immediately and does NOT send any context headers', async () => {
    renderDashboard();
    fireEvent.click(screen.getByRole('button', { name: /^start$/i }));
    await waitFor(() => {
      expect(mockControlMutate).toHaveBeenCalled();
    });
    const call = mockControlMutate.mock.calls[0][0];
    expect(call.variables).toEqual({ command: 'start', payload: {} });
    // #2884: no X-MFA-Token header — the MFA re-auth placeholder was removed.
    expect(call.context).toBeUndefined();
  });

  it('Stop is graceful — fires immediately without a confirm modal (#2884)', async () => {
    renderDashboard();
    fireEvent.click(screen.getByRole('button', { name: /^stop$/i }));
    await waitFor(() => {
      expect(mockControlMutate).toHaveBeenCalled();
    });
    const call = mockControlMutate.mock.calls[0][0];
    expect(call.variables).toEqual({ command: 'stop', payload: {} });
    // No confirm dialog and no X-MFA-Token header.
    expect(call.context).toBeUndefined();
  });

  it('Force Stop requires confirm and fires without X-MFA-Token (#2884)', async () => {
    renderDashboard();
    fireEvent.click(screen.getByRole('button', { name: /force stop/i }));
    // ConfirmDialog appears — the mutation has NOT fired yet.
    expect(mockControlMutate).not.toHaveBeenCalled();
    const confirm = await screen.findByTestId('confirm-dialog-confirm');
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(mockControlMutate).toHaveBeenCalled();
    });
    const call = mockControlMutate.mock.calls[0][0];
    expect(call.variables.command).toBe('force_stop');
    expect(call.context).toBeUndefined();
  });

  it('Pause/Resume/Drain buttons no longer exist (#2884 removed)', () => {
    renderDashboard();
    expect(screen.queryByRole('button', { name: /^pause$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^resume$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /stop \(drain\)/i })).toBeNull();
  });

  it('dispatcher-controls has exactly three buttons: Start, Stop, Force Stop (#2884 AC2)', () => {
    renderDashboard();
    const controls = screen.getByTestId('dispatcher-controls');
    const buttons = controls.querySelectorAll('button');
    expect(buttons.length).toBe(3);
    expect(buttons[0].textContent).toMatch(/^start$/i);
    expect(buttons[1].textContent).toMatch(/^stop$/i);
    expect(buttons[2].textContent).toMatch(/^force stop$/i);
  });
});

describe('DispatcherDashboard — config edit lifecycle', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('raising concurrency_cap commits immediately without any context headers (#2884)', async () => {
    renderDashboard();
    const capButton = screen.getByTestId('config-value-concurrency_cap');
    fireEvent.click(capButton);
    const input = screen.getByTestId('config-input-concurrency_cap') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '4' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => {
      expect(mockSetConfigMutate).toHaveBeenCalled();
    });
    const call = mockSetConfigMutate.mock.calls[0][0];
    expect(call.variables).toEqual({ key: 'concurrency_cap', value: '4' });
    // #2884: no X-MFA-Token header — admin session auth is the gate.
    expect(call.context).toBeUndefined();
  });

  it('lowering concurrency_cap pops confirm dialog before committing (no MFA header)', async () => {
    renderDashboard();
    const capButton = screen.getByTestId('config-value-concurrency_cap');
    fireEvent.click(capButton);
    const input = screen.getByTestId('config-input-concurrency_cap') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '1' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    // Dialog is open — mutation has NOT fired.
    const confirm = await screen.findByTestId('confirm-dialog-confirm');
    expect(mockSetConfigMutate).not.toHaveBeenCalled();
    // Dialog copy mentions the transition.
    expect(screen.getByText(/lower concurrency cap/i)).toBeInTheDocument();
    expect(screen.getByText(/from 3 to 1/i)).toBeInTheDocument();
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(mockSetConfigMutate).toHaveBeenCalled();
    });
    const call = mockSetConfigMutate.mock.calls[0][0];
    expect(call.variables).toEqual({ key: 'concurrency_cap', value: '1' });
    // #2884: no X-MFA-Token header.
    expect(call.context).toBeUndefined();
  });

  it('Escape cancels an in-progress cap edit without firing the mutation', async () => {
    renderDashboard();
    const capButton = screen.getByTestId('config-value-concurrency_cap');
    fireEvent.click(capButton);
    const input = screen.getByTestId('config-input-concurrency_cap') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '2' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    // Back to the display button — no mutation fired.
    expect(screen.getByTestId('config-value-concurrency_cap')).toBeInTheDocument();
    expect(mockSetConfigMutate).not.toHaveBeenCalled();
  });

  it('backoff edit commits without MFA header + preserves higher attempts in array (#2884)', async () => {
    renderDashboard();
    const backoffButton = screen.getByTestId('config-value-backoff_seconds');
    fireEvent.click(backoffButton);
    const input = screen.getByTestId('config-input-backoff_seconds') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '120' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => {
      expect(mockSetConfigMutate).toHaveBeenCalled();
    });
    const call = mockSetConfigMutate.mock.calls[0][0];
    expect(call.variables.key).toBe('backoff_seconds');
    // First slot updated; subsequent retry delays preserved.
    expect(JSON.parse(call.variables.value)).toEqual([120, 300, 900]);
    // #2884: no X-MFA-Token header.
    expect(call.context).toBeUndefined();
  });
});

// #4063 — `config` was moved out of the polled `dispatcherState` query into
// a separate `DispatcherConfig` query that fires once on mount and is
// refetched explicitly after a `dispatcherSetConfig` mutation. Two
// regressions to guard:
//   1. The polled `DispatcherState` query MUST NOT select `config` (saves
//      ~40 cost units against the #4003 1000-cap).
//   2. The new `DispatcherConfig` query is fired without `pollInterval`
//      and is refetched (not the polled state query) after a config edit.
describe('DispatcherDashboard — #4063 config decoupled from poll', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('DispatcherState query is polled (pollInterval set) but does NOT carry config in its document', async () => {
    renderDashboard();
    // The state query must have been invoked with a non-zero pollInterval —
    // that's the contract that drives the 2s real-time refresh.
    const stateCalls = useQueryCalls['DispatcherState'] ?? [];
    expect(stateCalls.length).toBeGreaterThan(0);
    expect(stateCalls.every((c) => (c.pollInterval ?? 0) > 0)).toBe(true);

    // The query document MUST NOT select `config` — this is the cost-cap
    // regression guard. We re-import the query and walk its AST.
    const queries = await import('@/lib/dispatcher-queries');
    const stateDoc = queries.DISPATCHER_STATE_QUERY;
    const stateText = JSON.stringify(stateDoc);
    expect(stateText).not.toMatch(/"name":\{[^}]*"value":"config"/);
  });

  it('DispatcherConfig query fires WITHOUT pollInterval (fires once on mount)', () => {
    renderDashboard();
    const configCalls = useQueryCalls['DispatcherConfig'] ?? [];
    // The query must have been invoked at least once.
    expect(configCalls.length).toBeGreaterThan(0);
    // None of those invocations may carry a pollInterval — `config`
    // changes rarely (manual operator edits) and doesn't ride the 2s
    // real-time refresh.
    for (const call of configCalls) {
      expect(call.pollInterval ?? 0).toBe(0);
    }
  });

  it('committing a config edit refetches DispatcherConfig (not the polled state query)', async () => {
    renderDashboard();
    // Sanity: no refetches yet.
    expect(mockConfigRefetch).not.toHaveBeenCalled();
    expect(mockStateRefetch).not.toHaveBeenCalled();

    const capButton = screen.getByTestId('config-value-concurrency_cap');
    fireEvent.click(capButton);
    const input = screen.getByTestId('config-input-concurrency_cap') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '4' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(mockSetConfigMutate).toHaveBeenCalled();
    });
    // Refetch is awaited inside `commitConfigEdit` — wait for it.
    await waitFor(() => {
      expect(mockConfigRefetch).toHaveBeenCalled();
    });
    // The polled-state refetch must NOT have been triggered by the
    // config edit — the polled query no longer carries `config`.
    expect(mockStateRefetch).not.toHaveBeenCalled();
  });

  it('ConfigPanel reads entries from the DispatcherConfig query (not from polled state)', () => {
    // Override the config query result with a value the polled state
    // does NOT carry. If the panel is wired correctly, the displayed cap
    // tracks the dedicated config query.
    mockConfigData = {
      dispatcherState: {
        config: [
          {
            key: 'concurrency_cap',
            value: '7',
            updatedAt: '2026-04-18T09:00:00Z',
            updatedBy: 'init',
          },
          {
            key: 'backoff_seconds',
            value: '[60,300,900]',
            updatedAt: '2026-04-18T09:00:00Z',
            updatedBy: 'init',
          },
        ],
      },
    };
    // Polled state still has the OLD value (3) — used to be where the
    // panel read from. After #4063 it must NOT be the source of truth.
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
    renderDashboard();
    const capButton = screen.getByTestId('config-value-concurrency_cap');
    // Display reads as "[7]" — pulled from the new query, not the old state.
    expect(capButton.textContent).toMatch(/\[7\]/);
    expect(capButton.textContent).not.toMatch(/\[3\]/);
  });
});

describe('DispatcherDashboard — #2823 state-flow two-column layout', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('renders both columns inside a single `lg:grid-cols-2` deck', () => {
    renderDashboard();
    const deck = screen.getByTestId('dispatcher-two-column-deck');
    // Outer container is a 1-col grid on mobile, 2-col grid at `lg`.
    expect(deck.className).toMatch(/grid-cols-1/);
    expect(deck.className).toMatch(/lg:grid-cols-2/);
    // Both columns are present.
    expect(screen.getByTestId('dispatcher-column-left')).toBeInTheDocument();
    expect(screen.getByTestId('dispatcher-column-right')).toBeInTheDocument();
  });

  it('left column is a vertical flex stack of Active agents (top) then Queue: Agent-ready (bottom)', () => {
    renderDashboard();
    const left = screen.getByTestId('dispatcher-column-left');
    // Column uses `flex flex-col gap-*` for variable-height panels, not a
    // fixed-row grid (#2823: panels have natural heights).
    expect(left.className).toMatch(/\bflex\b/);
    expect(left.className).toMatch(/flex-col/);
    // Locate the Active and Ready section headings within the column.
    const active = left.querySelector('#active-agents-heading');
    const ready = left.querySelector('#queue-ready-heading');
    expect(active).not.toBeNull();
    expect(ready).not.toBeNull();
    // Active must come before Ready in DOM order.
    expect(active!.compareDocumentPosition(ready!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Blocked is NOT in the left column.
    expect(left.querySelector('#queue-blocked-heading')).toBeNull();
  });

  it('right column is a vertical flex stack of Recently completed (top) then Queue: Blocked (bottom)', () => {
    renderDashboard();
    const right = screen.getByTestId('dispatcher-column-right');
    expect(right.className).toMatch(/\bflex\b/);
    expect(right.className).toMatch(/flex-col/);
    const completed = right.querySelector('#recent-completions-heading');
    const blocked = right.querySelector('#queue-blocked-heading');
    expect(completed).not.toBeNull();
    expect(blocked).not.toBeNull();
    expect(
      completed!.compareDocumentPosition(blocked!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Ready is NOT in the right column.
    expect(right.querySelector('#queue-ready-heading')).toBeNull();
  });

  it('on mobile collapse (single column), panel DOM order is Active → Queue-ready → Recently-completed → Queue-blocked', () => {
    // The CSS grid reflows to a single column at `< lg`, but the
    // document order is stable and determines the mobile stack. Assert
    // the four section headings appear in the required order.
    renderDashboard();
    const deck = screen.getByTestId('dispatcher-two-column-deck');
    const headings = Array.from(
      deck.querySelectorAll(
        '#active-agents-heading, #queue-ready-heading, #recent-completions-heading, #queue-blocked-heading',
      ),
    ).map((el) => el.id);
    expect(headings).toEqual([
      'active-agents-heading',
      'queue-ready-heading',
      'recent-completions-heading',
      'queue-blocked-heading',
    ]);
  });
});

describe('DispatcherDashboard — #2860 circuit-breaker banner', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
  });

  it('renders the banner when circuitBreakerOpen is true', () => {
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        circuitBreakerOpen: true,
        capFlippedBy: 'circuit_breaker',
      },
    };
    renderDashboard();
    const banner = screen.getByTestId('circuit-breaker-banner');
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).toMatch(/Circuit breaker open/i);
    expect(banner.textContent).toMatch(/concurrency_cap/);
    // Red by design — the circuit breaker is a loud signal.
    expect(banner.className).toMatch(/bg-red/);
    expect(banner.getAttribute('role')).toBe('alert');
  });

  it('does not render the banner when circuitBreakerOpen is false', () => {
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        circuitBreakerOpen: false,
        capFlippedBy: null,
      },
    };
    renderDashboard();
    expect(screen.queryByTestId('circuit-breaker-banner')).not.toBeInTheDocument();
  });

  it('does not render the banner when operator-paused (capFlippedBy != "circuit_breaker")', () => {
    // Operator hit Pause — concurrency_cap is 0, but circuitBreakerOpen is
    // false because the breaker did not trip it. The banner must stay
    // hidden so the operator does not see a misleading "circuit open"
    // signal for their own deliberate pause.
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        circuitBreakerOpen: false,
        capFlippedBy: 'operator',
      },
    };
    renderDashboard();
    expect(screen.queryByTestId('circuit-breaker-banner')).not.toBeInTheDocument();
  });
});

// #2967 originally wrapped poll state updates in document.startViewTransition
// to drive Magic Move animations. #3584 reverted that because the root
// viewport snapshot caused operator-visible header flicker on every 2s poll.
// The data-mirror effect now always updates synchronously.
describe('DispatcherDashboard — #3584 synchronous poll state update (no view-transition)', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
  });

  it('does NOT call startViewTransition on a poll-driven update (#3584)', async () => {
    // Install the stub — post-fix, it must never be called.
    const startViewTransition = vi.fn((cb: () => void) => {
      cb();
      return { finished: Promise.resolve() };
    });
    (
      document as unknown as {
        startViewTransition?: (cb: () => void) => unknown;
      }
    ).startViewTransition = startViewTransition;

    const originalMatchMedia = window.matchMedia;
    (window as unknown as { matchMedia: typeof window.matchMedia }).matchMedia =
      ((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      })) as typeof window.matchMedia;

    try {
      mockQueryData = { dispatcherState: { ...BASE_STATE } };
      const { rerender } = renderDashboard();
      expect(startViewTransition).not.toHaveBeenCalled();

      // Simulate a poll landing a *new* data reference.
      mockQueryData = {
        dispatcherState: {
          ...BASE_STATE,
          queueDepth: 99,
        },
      };
      rerender(<DispatcherDashboard />);

      // Wait for the effect to run and verify state was updated
      // (queueDepth reflected in the badge).
      await waitFor(() => {
        expect(screen.getByTestId('queue-ready-count')).toHaveTextContent('0 / 99');
      });
      // startViewTransition must never have been called — the update is
      // now synchronous per #3584.
      expect(startViewTransition).not.toHaveBeenCalled();
    } finally {
      delete (
        document as unknown as {
          startViewTransition?: (cb: () => void) => unknown;
        }
      ).startViewTransition;
      (
        window as unknown as { matchMedia: typeof window.matchMedia }
      ).matchMedia = originalMatchMedia;
    }
  });

  it('renders updated poll data without startViewTransition (Firefox-like env)', async () => {
    delete (
      document as unknown as {
        startViewTransition?: (cb: () => void) => unknown;
      }
    ).startViewTransition;
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        queueDepth: 7,
      },
    };
    renderDashboard();
    await waitFor(() => {
      expect(screen.getByTestId('queue-ready-count')).toHaveTextContent('0 / 7');
    });
  });
});

describe('DispatcherDashboard — #3159 expand-count dialog wiring', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
    mockQueueFullData.READY = undefined;
    mockQueueFullData.BLOCKED = undefined;
    mockQueueFullData.COMPLETED = undefined;
  });

  it('renders all three count badges as buttons (Ready, Blocked, Recently Completed)', () => {
    renderDashboard();
    expect(screen.getByTestId('queue-ready-count').tagName).toBe('BUTTON');
    expect(screen.getByTestId('queue-blocked-count').tagName).toBe('BUTTON');
    expect(screen.getByTestId('recent-completions-count').tagName).toBe(
      'BUTTON',
    );
  });

  it('clicking the Ready count opens the dialog with the READY title', async () => {
    mockQueueFullData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: [],
        completions: [],
      },
    };
    renderDashboard();
    expect(screen.queryByTestId('queue-full-dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('queue-ready-count'));
    const title = await screen.findByTestId('queue-full-dialog-title');
    // #3172: title now matches the panel heading "Queue: Agent-ready"
    // exactly (was "Agent-ready queue (N)" pre-#3172).
    expect(title.textContent).toMatch(/queue: agent-ready/i);
  });

  it('clicking the Blocked count opens the dialog with the BLOCKED title', async () => {
    mockQueueFullData.BLOCKED = {
      dispatcherQueueFull: {
        kind: 'BLOCKED',
        queueItems: [],
        completions: [],
      },
    };
    renderDashboard();
    fireEvent.click(screen.getByTestId('queue-blocked-count'));
    const title = await screen.findByTestId('queue-full-dialog-title');
    // #3172: title now matches the panel heading "Queue: Blocked" exactly
    // (was "Blocked queue (N)" pre-#3172).
    expect(title.textContent).toMatch(/queue: blocked/i);
  });

  it('clicking the Recently Completed count opens the dialog with the COMPLETED title', async () => {
    mockQueueFullData.COMPLETED = {
      dispatcherQueueFull: {
        kind: 'COMPLETED',
        queueItems: [],
        completions: [],
      },
    };
    renderDashboard();
    fireEvent.click(screen.getByTestId('recent-completions-count'));
    const title = await screen.findByTestId('queue-full-dialog-title');
    expect(title.textContent).toMatch(/recently completed/i);
  });

  it('dialog does NOT open by default and does NOT prefetch on mount', () => {
    renderDashboard();
    // No dialog DOM, no list rendered.
    expect(screen.queryByTestId('queue-full-dialog')).not.toBeInTheDocument();
    expect(screen.queryByTestId('queue-full-dialog-list')).not.toBeInTheDocument();
  });

  it('dialog renders the full list with no 10-cap (READY, 25 items)', async () => {
    const items = Array.from({ length: 25 }, (_, i) => ({
      issueNumber: 9000 + i,
      title: `issue ${9000 + i}`,
      priority: 'p2',
      labels: ['priority/p2', 'agent/ready'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [],
      cooldownSecondsRemaining: null,
    }));
    mockQueueFullData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: items,
        completions: [],
      },
    };
    renderDashboard();
    fireEvent.click(screen.getByTestId('queue-ready-count'));
    await screen.findByTestId('queue-full-dialog-list');
    // 25 issue links rendered — well past the 10-cap.
    for (const it of items) {
      expect(
        screen.getByTestId(`issue-link-${it.issueNumber}`),
      ).toBeInTheDocument();
    }
  });
});

// #3172 threaded `{shown} / {total}` into the dialog title; #3222
// reverted the numerator because the dialog renders every row (no
// 10-cap). These tests assert the bare-total shape while verifying the
// panel badge keeps its `{shown} / {total}` format (still correct for
// the 10-row-capped panel context).
describe('DispatcherDashboard — #3222 dialog title (total only)', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueueFullData.READY = undefined;
    mockQueueFullData.BLOCKED = undefined;
    mockQueueFullData.COMPLETED = undefined;
  });

  it('READY dialog title reads `Queue: Agent-ready — {total}` (panel badge still shows {shown} / {total})', async () => {
    // Panel renders 10 (capped); the daemon has scanned 50 ready issues.
    const items = Array.from({ length: 10 }, (_, i) => ({
      issueNumber: 8000 + i,
      title: `ready ${8000 + i}`,
      priority: 'p2',
      labels: ['priority/p2', 'agent/ready'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [],
      cooldownSecondsRemaining: null,
    }));
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        queueDepth: 50,
        queueReady: items,
      },
    };
    mockQueueFullData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: items,
        completions: [],
      },
    };
    renderDashboard();
    // Sanity: panel badge still shows `10 / 50` (existing #2886
    // behaviour — the 10-row cap means the numerator is still
    // meaningful in the panel context, unlike the dialog).
    expect(screen.getByTestId('queue-ready-count')).toHaveTextContent(
      '10 / 50',
    );
    fireEvent.click(screen.getByTestId('queue-ready-count'));
    const title = await screen.findByTestId('queue-full-dialog-title');
    expect(title.textContent).toBe('Queue: Agent-ready — 50');
  });

  it('BLOCKED dialog title reads `Queue: Blocked — {total}`', async () => {
    const items = Array.from({ length: 4 }, (_, i) => ({
      issueNumber: 7000 + i,
      title: `blocked ${7000 + i}`,
      priority: 'p2',
      labels: ['priority/p2', 'status/blocked'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [42],
      cooldownSecondsRemaining: null,
    }));
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        blockedDepth: 12,
        queueBlocked: items,
      },
    };
    mockQueueFullData.BLOCKED = {
      dispatcherQueueFull: {
        kind: 'BLOCKED',
        queueItems: items,
        completions: [],
      },
    };
    renderDashboard();
    expect(screen.getByTestId('queue-blocked-count')).toHaveTextContent(
      '4 / 12',
    );
    fireEvent.click(screen.getByTestId('queue-blocked-count'));
    const title = await screen.findByTestId('queue-full-dialog-title');
    expect(title.textContent).toBe('Queue: Blocked — 12');
  });

  it('COMPLETED dialog title reads `Recently completed — {total}`', async () => {
    const completions = Array.from({ length: 10 }, (_, i) => ({
      agentId: `aaaaaaaa-bbbb-cccc-dddd-${String(i).padStart(12, '0')}`,
      issueNumber: 6000 + i,
      issueTitle: `done ${6000 + i}`,
      priority: 'p2',
      status: 'succeeded',
      startedAt: '2026-04-18T11:50:00Z',
      endedAt: '2026-04-18T11:55:00Z',
      prNumber: 7000 + i,
      totalTokens: null,
      totalCostUsd: null,
      failureSummary: null,
      mergedAt: '2026-04-18T11:50:00Z',
      verifiedAt: '2026-04-18T11:53:00Z',
      verifySkipReason: null,
      retroedAt: '2026-04-18T11:55:00Z',
    }));
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        recentCompletions: completions,
        recentCompletionsCount: 183,
      },
    };
    mockQueueFullData.COMPLETED = {
      dispatcherQueueFull: {
        kind: 'COMPLETED',
        queueItems: [],
        completions,
      },
    };
    renderDashboard();
    expect(screen.getByTestId('recent-completions-count')).toHaveTextContent(
      '10 / 183',
    );
    fireEvent.click(screen.getByTestId('recent-completions-count'));
    const title = await screen.findByTestId('queue-full-dialog-title');
    expect(title.textContent).toBe('Recently completed — 183');
  });
});

// #2812: Phase 2 polish — vertical density + IssueLink for #2801.
describe('DispatcherDashboard — #2812 Phase 2 polish', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('§2.1 — grid uses gap-y-4', () => {
    renderDashboard();
    const deck = screen.getByTestId('dispatcher-two-column-deck');
    expect(deck.className).toMatch(/gap-x-6 gap-y-4/);
  });

  it('§2.1 — column wrappers use gap-4', () => {
    renderDashboard();
    const left = screen.getByTestId('dispatcher-column-left');
    const right = screen.getByTestId('dispatcher-column-right');
    expect(left.className).toMatch(/gap-4/);
    expect(right.className).toMatch(/gap-4/);
  });

  it('§2.5 — #2990: stale #2801 footnote has been removed', () => {
    // The footnote "Daemon command handlers tracked in #2801…" was removed
    // in #2990 because #2801 closed on 2026-04-19 when PR #2858 landed
    // the daemon-side handlers. The disclaimer was wrong and misleading.
    renderDashboard();
    expect(screen.queryByTestId('issue-link-2801')).toBeNull();
    expect(screen.queryByText(/daemon command handlers/i)).toBeNull();
  });
});

// #3206: Magic Move view-transition-names on cockpit panel rows must be
// SUPPRESSED whenever any full-list dialog is open, otherwise the
// browser's view-transition compositor layer paints over the Radix
// dialog content (see issue body for root-cause analysis).
describe('DispatcherDashboard — #3206 dialog-open view-transition gate', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueueFullData.READY = undefined;
    mockQueueFullData.BLOCKED = undefined;
    mockQueueFullData.COMPLETED = undefined;
  });

  const baseReady = [
    {
      issueNumber: 3151,
      title: 'ready-row-1',
      priority: 'p2',
      labels: ['priority/p2', 'agent/ready'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [],
      cooldownSecondsRemaining: null,
    },
  ];
  const baseCompletions = [
    {
      agentId: 'aabbccdd-eeff-0011-2233-445566778899',
      issueNumber: 2805,
      issueTitle: 'recent-completion',
      priority: 'p2',
      status: 'succeeded',
      startedAt: '2026-04-18T11:50:00Z',
      endedAt: '2026-04-18T11:55:00Z',
      prNumber: 2811,
      totalTokens: null,
      totalCostUsd: null,
      failureSummary: null,
      mergedAt: '2026-04-18T11:50:00Z',
      verifiedAt: '2026-04-18T11:53:00Z',
      verifySkipReason: null,
      retroedAt: '2026-04-18T11:55:00Z',
    },
  ];
  const baseActive = [
    {
      id: 'aabbccdd-eeff-0011-2233-445566778899',
      issueNumber: 3100,
      issueTitle: 'active-agent',
      priority: 'p2',
      worktreePath: '/Users/x/.claude/worktrees/agent-aabbccdd',
      phase: 'ralph',
      status: 'running',
      startedAt: '2026-04-18T11:00:00Z',
      endedAt: null,
      exitCode: null,
      prNumber: null,
      retriesUsed: 0,
    },
  ];

  it('ready rows carry view-transition-name when no dialog is open', () => {
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        queueReady: baseReady,
        queueDepth: 1,
      },
    };
    renderDashboard();
    const row = screen.getByTestId('queue-row-3151');
    expect((row as HTMLElement).style.viewTransitionName).toBe('issue-3151');
  });

  it('opening the Ready dialog strips view-transition-name from Ready rows', async () => {
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        queueReady: baseReady,
        queueDepth: 1,
      },
    };
    mockQueueFullData.READY = {
      dispatcherQueueFull: {
        kind: 'READY',
        queueItems: baseReady,
        completions: [],
      },
    };
    renderDashboard();
    // Sanity: pre-click the row carries the name.
    const rowBefore = screen.getByTestId('queue-row-3151');
    expect((rowBefore as HTMLElement).style.viewTransitionName).toBe(
      'issue-3151',
    );
    fireEvent.click(screen.getByTestId('queue-ready-count'));
    await screen.findByTestId('queue-full-dialog');
    // After the dialog opens, the ready row is re-rendered without a
    // `viewTransitionName` — it's no longer a queue-row-<N> testid'd
    // element (the row only carries the testid when `animated`).
    expect(screen.queryByTestId('queue-row-3151')).toBeNull();
  });

  it('opening the Blocked dialog ALSO strips Ready rows and Active / Completed transitions', async () => {
    // All three other panels' rows need to stop animating when any
    // dialog is open — the dialog is modal over the full cockpit, not
    // just its own column. Exercise this by opening the BLOCKED dialog
    // (which has no animated rows itself) and confirming the OTHER
    // panels' rows lose their view-transition-names.
    mockQueryData = {
      dispatcherState: {
        ...BASE_STATE,
        queueReady: baseReady,
        queueDepth: 1,
        queueBlocked: [],
        blockedDepth: 3,
        activeAgents: baseActive,
        recentCompletions: baseCompletions,
        recentCompletionsCount: 1,
      },
    };
    mockQueueFullData.BLOCKED = {
      dispatcherQueueFull: {
        kind: 'BLOCKED',
        queueItems: [],
        completions: [],
      },
    };
    renderDashboard();
    // Pre-click: all three animated rows have their names.
    expect(
      (screen.getByTestId('queue-row-3151') as HTMLElement).style
        .viewTransitionName,
    ).toBe('issue-3151');
    expect(
      (
        screen.getByTestId(
          'active-agent-row-aabbccdd-eeff-0011-2233-445566778899',
        ) as HTMLElement
      ).style.viewTransitionName,
    ).toBe('issue-3100');
    expect(
      (
        screen.getByTestId(
          'completion-row-aabbccdd-eeff-0011-2233-445566778899',
        ) as HTMLElement
      ).style.viewTransitionName,
    ).toBe('agent-aabbccdd-eeff-0011-2233-445566778899');

    fireEvent.click(screen.getByTestId('queue-blocked-count'));
    await screen.findByTestId('queue-full-dialog');

    // Post-click: Active and Completed row names are gone; Ready row
    // drops the `queue-row-<N>` testid entirely (animated=false).
    expect(screen.queryByTestId('queue-row-3151')).toBeNull();
    expect(
      (
        screen.getByTestId(
          'active-agent-row-aabbccdd-eeff-0011-2233-445566778899',
        ) as HTMLElement
      ).style.viewTransitionName,
    ).toBe('');
    expect(
      (
        screen.getByTestId(
          'completion-row-aabbccdd-eeff-0011-2233-445566778899',
        ) as HTMLElement
      ).style.viewTransitionName,
    ).toBe('');
  });
});

describe('DispatcherDashboard — DiagnoserEffectivenessPanel (#2800)', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('mounts the DiagnoserEffectivenessPanel (empty-state copy visible)', () => {
    // The WeeklyDiagnoserReport mock in useQuery returns [] so the
    // empty-state copy should render below the RecentFailuresPanel.
    renderDashboard();
    expect(
      screen.getByText(/diagnoser effectiveness/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/no diagnoses with resolved outcomes yet/i),
    ).toBeInTheDocument();
  });
});

// #3220 originally tested that the root view-transition wrapper was skipped
// while a dialog was open. #3584 reverted the view-transition wrapping on
// polls entirely — startViewTransition is never called on any poll update.
// The dialog-open / dialog-closed distinction no longer affects the poll
// path; the per-row `view-transition-name` suppression (#3206) is still
// tested in the '#3206 dialog-open view-transition gate' describe above.
describe('DispatcherDashboard — #3220 / #3584 no view-transition on any poll (with or without dialog)', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockStateRefetch.mockClear();
    mockConfigRefetch.mockClear();
    mockConfigData = undefined;
    for (const k of Object.keys(useQueryCalls)) delete useQueryCalls[k];
    mockQueueFullData.READY = undefined;
    mockQueueFullData.BLOCKED = undefined;
    mockQueueFullData.COMPLETED = undefined;
  });

  it('does NOT call startViewTransition on a poll update while a dialog is open', async () => {
    const startViewTransition = vi.fn((cb: () => void): unknown => {
      cb();
      return { finished: Promise.resolve() };
    });
    (
      document as unknown as {
        startViewTransition?: (cb: () => void) => unknown;
      }
    ).startViewTransition = startViewTransition;

    try {
      mockQueryData = { dispatcherState: { ...BASE_STATE } };
      mockQueueFullData.READY = {
        dispatcherQueueFull: { kind: 'READY', queueItems: [], completions: [] },
      };
      const { rerender } = renderDashboard();
      fireEvent.click(screen.getByTestId('queue-ready-count'));
      await screen.findByTestId('queue-full-dialog');

      mockQueryData = { dispatcherState: { ...BASE_STATE, queueDepth: 42 } };
      rerender(<DispatcherDashboard />);

      await waitFor(() => {
        expect(screen.queryByTestId('queue-full-dialog')).not.toBeNull();
      });
      expect(startViewTransition).not.toHaveBeenCalled();
    } finally {
      delete (
        document as unknown as {
          startViewTransition?: (cb: () => void) => unknown;
        }
      ).startViewTransition;
    }
  });

  it('does NOT call startViewTransition on a poll update when no dialog is open (#3584)', async () => {
    const startViewTransition = vi.fn((cb: () => void): unknown => {
      cb();
      return { finished: Promise.resolve() };
    });
    (
      document as unknown as {
        startViewTransition?: (cb: () => void) => unknown;
      }
    ).startViewTransition = startViewTransition;

    try {
      mockQueryData = { dispatcherState: { ...BASE_STATE } };
      const { rerender } = renderDashboard();
      expect(startViewTransition).not.toHaveBeenCalled();

      mockQueryData = { dispatcherState: { ...BASE_STATE, queueDepth: 42 } };
      rerender(<DispatcherDashboard />);

      await waitFor(() => {
        expect(screen.getByTestId('queue-ready-count')).toHaveTextContent('0 / 42');
      });
      expect(startViewTransition).not.toHaveBeenCalled();
    } finally {
      delete (
        document as unknown as {
          startViewTransition?: (cb: () => void) => unknown;
        }
      ).startViewTransition;
    }
  });
});
