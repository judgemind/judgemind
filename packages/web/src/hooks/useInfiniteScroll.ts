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
  /**
   * IntersectionObserver `rootMargin` for pre-fetching.
   * @default '200px'
   */
  rootMargin?: string;
}

/**
 * Return value of the `useInfiniteScroll` hook.
 */
export interface UseInfiniteScrollReturn {
  /**
   * Ref callback to attach to the sentinel element. The hook manages
   * the IntersectionObserver lifecycle and triggers `fetchMore` when
   * the sentinel scrolls into view.
   */
  sentinelRef: (node: HTMLElement | null) => void;
  /**
   * The load-more handler, exposed for testing or manual triggering.
   * Normally called automatically by the IntersectionObserver.
   */
  handleLoadMore: () => void;
}

/**
 * A reusable hook that encapsulates the infinite scroll pattern:
 *
 * 1. Manages a `fetchingMore` ref to prevent duplicate in-flight requests.
 * 2. Optionally tracks a query generation counter to discard stale results
 *    when filters change mid-flight.
 * 3. Sets up an IntersectionObserver on the sentinel element and calls
 *    `fetchMore` when it enters the viewport.
 *
 * Usage:
 * ```tsx
 * const { sentinelRef } = useInfiniteScroll({
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
 * // In JSX — render the sentinel only when there are more pages and
 * // not currently loading:
 * {!loading && hasNextPage && <div ref={sentinelRef} className="h-1" />}
 * ```
 */
export function useInfiniteScroll<TData>({
  hasNextPage,
  endCursor,
  loading,
  fetchMore,
  merge,
  filterDeps,
  rootMargin = '200px',
}: UseInfiniteScrollOptions<TData>): UseInfiniteScrollReturn {
  const fetchingMore = useRef(false);
  const observer = useRef<IntersectionObserver | null>(null);
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

  const sentinelRef = useCallback(
    (node: HTMLElement | null) => {
      if (observer.current) observer.current.disconnect();
      if (!node) return;
      observer.current = new IntersectionObserver(
        (entries) => {
          if (entries[0]?.isIntersecting) {
            handleLoadMore();
          }
        },
        { rootMargin },
      );
      observer.current.observe(node);
    },
    [handleLoadMore, rootMargin],
  );

  // Clean up observer on unmount.
  useEffect(() => {
    return () => {
      if (observer.current) observer.current.disconnect();
    };
  }, []);

  return { sentinelRef, handleLoadMore };
}
