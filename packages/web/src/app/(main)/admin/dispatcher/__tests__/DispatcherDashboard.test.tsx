/**
 * Tests for the dispatcher admin dashboard (#2805).
 *
 * Focus areas:
 *   - Destructive control mutations always inject `X-MFA-Token` in the
 *     Apollo `context.headers` (#2805 §1.1 / #2803 Option A).
 *   - Config-edit mutations always inject `X-MFA-Token`.
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

let mockQueryData: { dispatcherState?: Record<string, unknown> } | undefined;

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>('@apollo/client');
  return {
    ...actual,
    useQuery: () => ({
      data: mockQueryData,
      loading: false,
      error: undefined,
      refetch: mockRefetch,
    }),
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
  queueReady: [],
  queueBlocked: [],
  recentCompletions: [],
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

describe('DispatcherDashboard — MFA header plumbing', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('non-destructive commands (Pause) do NOT send X-MFA-Token', async () => {
    renderDashboard();
    fireEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => {
      expect(mockControlMutate).toHaveBeenCalled();
    });
    const call = mockControlMutate.mock.calls[0][0];
    expect(call.variables).toEqual({ command: 'pause', payload: {} });
    expect(call.context).toBeUndefined();
  });

  it('destructive Stop requires confirm + sends X-MFA-Token on confirm', async () => {
    renderDashboard();
    fireEvent.click(screen.getByRole('button', { name: /force-stop/i }));
    // ConfirmDialog appears — the mutation has NOT fired yet.
    expect(mockControlMutate).not.toHaveBeenCalled();
    const confirm = await screen.findByTestId('confirm-dialog-confirm');
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(mockControlMutate).toHaveBeenCalled();
    });
    const call = mockControlMutate.mock.calls[0][0];
    expect(call.variables.command).toBe('stop');
    expect(call.context?.headers).toEqual({ 'X-MFA-Token': 'phase1-placeholder' });
  });

  it('destructive Drain (stop drain) sends X-MFA-Token on confirm', async () => {
    renderDashboard();
    fireEvent.click(screen.getByRole('button', { name: /stop \(drain\)/i }));
    const confirm = await screen.findByTestId('confirm-dialog-confirm');
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(mockControlMutate).toHaveBeenCalled();
    });
    const call = mockControlMutate.mock.calls[0][0];
    expect(call.variables.command).toBe('drain');
    expect(call.context?.headers).toEqual({ 'X-MFA-Token': 'phase1-placeholder' });
  });
});

describe('DispatcherDashboard — config edit lifecycle', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
    mockQueryData = { dispatcherState: { ...BASE_STATE } };
  });

  it('raising concurrency_cap commits immediately with MFA header', async () => {
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
    expect(call.context?.headers).toEqual({ 'X-MFA-Token': 'phase1-placeholder' });
  });

  it('lowering concurrency_cap pops confirm dialog before committing', async () => {
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
    expect(call.context?.headers).toEqual({ 'X-MFA-Token': 'phase1-placeholder' });
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

  it('backoff edit commits with MFA header + preserves higher attempts in array', async () => {
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
    expect(call.context?.headers).toEqual({ 'X-MFA-Token': 'phase1-placeholder' });
  });
});

describe('DispatcherDashboard — #2823 state-flow two-column layout', () => {
  beforeEach(() => {
    mockControlMutate.mockClear();
    mockSetConfigMutate.mockClear();
    mockRefetch.mockClear();
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
    // Red by design — overnight-safety rail is a loud signal.
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
