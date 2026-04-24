/**
 * Tests for `usePollBackoff` — exponential backoff hook (#3141).
 *
 * Uses `vi.useFakeTimers()` and an injectable `jitter` seed to make
 * timing assertions deterministic. With `jitter = () => 1` the
 * multiplier is `1 + (1 - 0.5) * 0.5 = 1.25`, so intervals are
 * inflated by exactly 25% above the nominal value.
 *
 * With `jitter = () => 0.5` the multiplier is `1 + (0.5 - 0.5) * 0.5 = 1.0`,
 * making intervals exactly nominal (no jitter).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useQuery } from '@apollo/client';
import { usePollBackoff } from '../usePollBackoff';

// ---------------------------------------------------------------------------
// Apollo mock
// ---------------------------------------------------------------------------

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>('@apollo/client');
  return {
    ...actual,
    useQuery: vi.fn(),
  };
});

const mockUseQuery = vi.mocked(useQuery);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// A minimal DocumentNode that satisfies the type checker.
const FAKE_QUERY = { kind: 'Document', definitions: [] } as unknown as ReturnType<typeof import('@apollo/client').gql>;

// Build a stable mock refetch that resolves immediately.
function makeRefetch(impl?: () => Promise<unknown>): () => Promise<unknown> {
  return impl ?? vi.fn().mockResolvedValue({ data: {} });
}

// Setup a mockUseQuery that returns controllable data.
function setupQuery({
  data = { dispatcherState: {} },
  loading = false,
  error = undefined,
  refetch,
}: {
  data?: unknown;
  loading?: boolean;
  error?: Error | undefined;
  refetch?: () => Promise<unknown>;
} = {}) {
  const mockRefetch = refetch ?? makeRefetch();
  mockUseQuery.mockReturnValue({
    data,
    loading,
    error,
    refetch: mockRefetch,
    networkStatus: 7,
    called: true,
    client: {} as never,
    variables: undefined,
    observable: {} as never,
    previousData: undefined,
    startPolling: vi.fn(),
    stopPolling: vi.fn(),
    subscribeToMore: vi.fn(),
    fetchMore: vi.fn(),
    updateQuery: vi.fn(),
  } as unknown as ReturnType<typeof useQuery>);
  return mockRefetch;
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('usePollBackoff', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockUseQuery.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('initialises with isStale = false', () => {
    setupQuery();
    const { result } = renderHook(() =>
      usePollBackoff({ query: FAKE_QUERY, jitter: () => 0.5 }),
    );
    expect(result.current.isStale).toBe(false);
  });

  it('schedules the first refetch at ~2s (jitter=0.5 → exact 2000ms)', async () => {
    const mockRefetch = setupQuery();
    renderHook(() => usePollBackoff({ query: FAKE_QUERY, jitter: () => 0.5 }));

    expect(mockRefetch).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
    });

    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('backoff schedule: 2→4→8→16→30s cap (jitter=0.5, no jitter applied)', async () => {
    // Make the refetch always fail to exercise the backoff.
    const mockRefetch = vi.fn().mockRejectedValue(new Error('network error'));
    setupQuery({ refetch: mockRefetch });

    renderHook(() => usePollBackoff({ query: FAKE_QUERY, jitter: () => 0.5 }));

    const expectedNominalIntervals = [2000, 4000, 8000, 16000, 30000];

    for (const interval of expectedNominalIntervals) {
      await act(async () => {
        vi.advanceTimersByTime(interval);
        // Allow the rejected promise to settle.
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    // 5 failures → 5 refetch calls.
    expect(mockRefetch).toHaveBeenCalledTimes(5);
  });

  it('resets to 2s interval on success after failures', async () => {
    let callCount = 0;
    const mockRefetch = vi.fn().mockImplementation(() => {
      callCount += 1;
      if (callCount <= 2) {
        return Promise.reject(new Error('fail'));
      }
      return Promise.resolve({ data: {} });
    });
    setupQuery({ refetch: mockRefetch });

    renderHook(() => usePollBackoff({ query: FAKE_QUERY, jitter: () => 0.5 }));

    // First failure at 2s.
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });

    // Second failure at 4s.
    await act(async () => {
      vi.advanceTimersByTime(4000);
      await Promise.resolve();
      await Promise.resolve();
    });

    // Third call succeeds — interval should reset to 2s.
    await act(async () => {
      vi.advanceTimersByTime(8000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockRefetch).toHaveBeenCalledTimes(3);

    // After a success, the next refetch should be scheduled at 2000ms.
    const beforeCount = mockRefetch.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockRefetch).toHaveBeenCalledTimes(beforeCount + 1);
  });

  it('isStale remains false after one consecutive failure', async () => {
    let callCount = 0;
    const mockRefetch = vi.fn().mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.reject(new Error('single failure'));
      }
      return Promise.resolve({ data: {} });
    });
    setupQuery({ refetch: mockRefetch });

    const { result } = renderHook(() =>
      usePollBackoff({ query: FAKE_QUERY, jitter: () => 0.5 }),
    );

    // Advance to trigger the first poll (which fails).
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });

    // isStale must remain false after exactly one failure.
    expect(result.current.isStale).toBe(false);
  });

  it('isStale becomes true after two consecutive failures', async () => {
    const mockRefetch = vi.fn().mockRejectedValue(new Error('persistent fail'));
    setupQuery({ refetch: mockRefetch });

    const { result } = renderHook(() =>
      usePollBackoff({ query: FAKE_QUERY, jitter: () => 0.5 }),
    );

    // First failure at 2s.
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.isStale).toBe(false);

    // Second failure at 4s.
    await act(async () => {
      vi.advanceTimersByTime(4000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.isStale).toBe(true);
  });

  it('isStale resets to false after a successful poll following failures', async () => {
    let callCount = 0;
    const mockRefetch = vi.fn().mockImplementation(() => {
      callCount += 1;
      if (callCount <= 2) {
        return Promise.reject(new Error('fail'));
      }
      return Promise.resolve({ data: {} });
    });
    setupQuery({ refetch: mockRefetch });

    const { result } = renderHook(() =>
      usePollBackoff({ query: FAKE_QUERY, jitter: () => 0.5 }),
    );

    // Two consecutive failures → isStale becomes true.
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(4000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.isStale).toBe(true);

    // Third call succeeds → isStale resets.
    await act(async () => {
      vi.advanceTimersByTime(8000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.isStale).toBe(false);
  });

  it('passes skip=true to useQuery and does not schedule polls', async () => {
    const mockRefetch = setupQuery();
    renderHook(() =>
      usePollBackoff({ query: FAKE_QUERY, skip: true, jitter: () => 0.5 }),
    );

    await act(async () => {
      vi.advanceTimersByTime(10000);
      await Promise.resolve();
    });

    // refetch must not have been called.
    expect(mockRefetch).not.toHaveBeenCalled();
    // useQuery was still called (with skip: true).
    expect(mockUseQuery).toHaveBeenCalledWith(
      FAKE_QUERY,
      expect.objectContaining({ skip: true }),
    );
  });
});
