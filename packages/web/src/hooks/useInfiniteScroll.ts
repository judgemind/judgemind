'use client';

import { useRef, useCallback, useEffect } from 'react';
import type { DependencyList } from 'react';

/**
 * Options for the `useInfiniteScroll` hook.
 *
 * @typeParam TData - The shape of the GraphQL query result that `fetchMore` returns.
 */
export interface UseInfiniteScrollOptions<TData> {
  /** Whether the server reports more pages available. */
  hasNextPage: boolean | undefined;
  /** The cursor to pass as `after` in the next fetchMore call. */
  endCursor: string | null | undefined;
  /** Whether a fetch is currently in progress (from `useQuery`). */
  loading: boolean;
  /** The `fetchMore` function from Apollo's `useQuery`. */
  fetchMore: (options: {
    variables: { after: string };
    updateQuery: (prev: TData, opts: { fetchMoreResult: TData }) => TData;
  }) => Promise<unknown>;
  /**
   * Merge function that combines previous data with newly-fetched data.
   * Called inside `updateQuery`. Return the merged result.
   *
   * @param prev - The previous query result.
   * @param incoming - The new page of results from `fetchMore`.
   * @returns The merged result to store in the Apollo cache.
   */
  merge: (prev: TData, incoming: TData) => TData;
  /**
   * Optional dependency list for filter values. When any value changes,
   * a generation counter increments so that in-flight fetchMore calls
   * discard stale results (i.e., results fetched under old filters).
   */
  filterDeps?: DependencyList;
}

/**
 * Return value of the `useInfiniteScroll` hook.
 */
export interface UseInfiniteScrollReturn {
  /**
   * The load-more handler. Pass this to `InfiniteScrollTrigger`'s
   * `onLoadMore` prop. Includes concurrency protection (prevents
   * duplicate in-flight requests) and generation tracking (discards
   * stale results when filters change mid-flight).
   */
  handleLoadMore: () => void;
}

/**
 * A reusable hook that encapsulates the data-fetching side of infinite scroll:
 *
 * 1. Manages a `fetchingMore` ref to prevent duplicate in-flight requests.
 * 2. Optionally tracks a query generation counter to discard stale results
 *    when filters change mid-flight.
 *
 * This hook is the **data-fetching concern** of infinite scroll. Pair it with
 * the `InfiniteScrollTrigger` component, which handles the **rendering concern**
 * (IntersectionObserver lifecycle and sentinel element).
 *
 * Usage:
 * ```tsx
 * const { handleLoadMore } = useInfiniteScroll({
 *   hasNextPage: pageInfo?.hasNextPage,
 *   endCursor: pageInfo?.endCursor,
 *   loading,
 *   fetchMore,
 *   merge: (prev, incoming) => ({
 *     rulings: {
 *       ...incoming.rulings,
 *       edges: [...prev.rulings.edges, ...incoming.rulings.edges],
 *     },
 *   }),
 *   filterDeps: [county, dateFrom, dateTo],
 * });
 *
 * // In JSX — pair with InfiniteScrollTrigger:
 * <InfiniteScrollTrigger
 *   hasNextPage={pageInfo?.hasNextPage ?? false}
 *   loading={loading}
 *   onLoadMore={handleLoadMore}
 * />
 * ```
 */
export function useInfiniteScroll<TData>({
  hasNextPage,
  endCursor,
  loading,
  fetchMore,
  merge,
  filterDeps,
}: UseInfiniteScrollOptions<TData>): UseInfiniteScrollReturn {
  const fetchingMore = useRef(false);
  const queryGeneration = useRef(0);

  // Increment generation when filter deps change, so in-flight
  // fetchMore calls from the previous filter state are discarded.
  useEffect(() => {
    queryGeneration.current += 1;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, filterDeps ?? []);

  const handleLoadMore = useCallback(() => {
    if (!endCursor || loading || fetchingMore.current) return;
    fetchingMore.current = true;
    const currentGeneration = queryGeneration.current;
    fetchMore({
      variables: { after: endCursor },
      updateQuery(prev: TData, { fetchMoreResult }: { fetchMoreResult: TData }) {
        if (!fetchMoreResult) return prev;
        // Discard results if the generation changed (filters changed mid-flight)
        if (queryGeneration.current !== currentGeneration) return prev;
        return merge(prev, fetchMoreResult);
      },
    }).finally(() => {
      fetchingMore.current = false;
    });
  }, [endCursor, loading, fetchMore, merge]);

  return { handleLoadMore };
}
