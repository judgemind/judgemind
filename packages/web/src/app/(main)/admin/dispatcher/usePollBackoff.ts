'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@apollo/client';
import type { DocumentNode, OperationVariables, QueryResult } from '@apollo/client';

/**
 * Exponential backoff schedule for `usePollBackoff`. Steps in seconds:
 * 2 → 4 → 8 → 16 → 30 (cap). Each step doubles until the cap.
 */
const INITIAL_INTERVAL_MS = 2000;
const MAX_INTERVAL_MS = 30000;

/**
 * `isStale` fires when consecutive failures ≥ this count OR the gap
 * since the last successful poll exceeds `STALE_AGE_MS` (#3141 AC 4).
 */
const STALE_FAILURE_COUNT = 2;
const STALE_AGE_MS = 5000;

/**
 * Options for `usePollBackoff`. The `jitter` prop is injectable for
 * deterministic tests — pass a constant (e.g. 1) to disable randomness
 * while still exercising the jitter arithmetic path.
 */
export interface UsePollBackoffOptions<TData, TVariables extends OperationVariables> {
  query: DocumentNode;
  variables?: TVariables;
  skip?: boolean;
  fetchPolicy?: 'cache-and-network' | 'network-only' | 'cache-first';
  /**
   * Jitter factor seed. In production `Math.random()` is used; in tests
   * pass a constant in `[0, 1)` to make timing assertions deterministic.
   * The formula is: `interval * (1 + (jitter - 0.5) * 0.5)` which yields
   * ±25% of the nominal interval.
   */
  jitter?: () => number;
}

export interface UsePollBackoffResult<TData> {
  data: TData | undefined;
  loading: boolean;
  error: Error | undefined;
  /** True after 2 consecutive failures or >5s since the last success. */
  isStale: boolean;
  refetch: () => Promise<unknown>;
}

/**
 * Replacement for Apollo's built-in `pollInterval` that applies
 * exponential backoff on failure and resets to 2s on success.
 *
 * Backoff schedule (nominal, before jitter): 2→4→8→16→30s (cap).
 * Jitter: ±25% of the nominal interval (`Math.random()` by default,
 * injectable for deterministic tests).
 *
 * `isStale` is true when:
 *   - 2 or more consecutive poll failures have occurred, OR
 *   - more than 5s have elapsed since the last successful poll.
 *
 * On success the interval resets to 2s.
 *
 * Issue #3141 — dispatcher cockpit polling reliability.
 */
export function usePollBackoff<TData, TVariables extends OperationVariables = OperationVariables>({
  query,
  variables,
  skip = false,
  fetchPolicy = 'cache-and-network',
  jitter = Math.random,
}: UsePollBackoffOptions<TData, TVariables>): UsePollBackoffResult<TData> {
  const { data, loading, error, refetch } = useQuery<TData, TVariables>(query, {
    variables,
    skip,
    fetchPolicy,
    // Drive all refetches manually via setTimeout — disable Apollo's
    // built-in polling so we control the interval.
    pollInterval: 0,
  });

  // Track consecutive failures and the last success timestamp.
  const consecutiveFailuresRef = useRef(0);
  const lastSuccessAtRef = useRef<number>(Date.now());
  const currentIntervalMsRef = useRef(INITIAL_INTERVAL_MS);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMountedRef = useRef(true);

  // isStale is derived state: recomputed on each render from the refs.
  // We keep a piece of React state to force re-renders when either
  // condition changes.
  const [isStale, setIsStale] = useState(false);

  // Compute and apply the jittered next interval.
  const nextInterval = useCallback(
    (baseMs: number): number => {
      const jitterFactor = 1 + (jitter() - 0.5) * 0.5;
      return Math.round(baseMs * jitterFactor);
    },
    [jitter],
  );

  const scheduleRefetch = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
    }
    if (skip || !isMountedRef.current) return;

    const interval = nextInterval(currentIntervalMsRef.current);
    timerRef.current = setTimeout(async () => {
      if (!isMountedRef.current || skip) return;
      try {
        await refetch();
        // Success — reset backoff and failure count.
        consecutiveFailuresRef.current = 0;
        lastSuccessAtRef.current = Date.now();
        currentIntervalMsRef.current = INITIAL_INTERVAL_MS;
        setIsStale(false);
      } catch {
        // Failure — step up the backoff interval (cap at MAX_INTERVAL_MS).
        consecutiveFailuresRef.current += 1;
        currentIntervalMsRef.current = Math.min(
          currentIntervalMsRef.current * 2,
          MAX_INTERVAL_MS,
        );
        const stale =
          consecutiveFailuresRef.current >= STALE_FAILURE_COUNT ||
          Date.now() - lastSuccessAtRef.current > STALE_AGE_MS;
        setIsStale(stale);
      }
      // Schedule the next refetch regardless of success/failure.
      scheduleRefetch();
    }, interval);
  }, [skip, refetch, nextInterval]);

  // Start polling when `skip` becomes false; clear timer when it becomes
  // true or on unmount.
  useEffect(() => {
    isMountedRef.current = true;
    if (!skip) {
      scheduleRefetch();
    }
    return () => {
      isMountedRef.current = false;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skip]);

  return {
    data,
    loading,
    error: error as Error | undefined,
    isStale,
    refetch,
  };
}
