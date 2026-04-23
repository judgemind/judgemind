'use client';

import { useRef, useEffect, useCallback } from 'react';

/**
 * Props for the InfiniteScrollTrigger component.
 *
 * @param hasNextPage - Whether more data is available to load.
 * @param loading - Whether a fetch is currently in progress.
 * @param onLoadMore - Callback invoked when the sentinel enters the viewport.
 * @param rootMargin - IntersectionObserver rootMargin (default: '200px').
 */
export interface InfiniteScrollTriggerProps {
  hasNextPage: boolean;
  loading: boolean;
  onLoadMore: () => void;
  rootMargin?: string;
}

/**
 * A shared infinite scroll sentinel component using IntersectionObserver.
 *
 * Renders a tiny invisible div that triggers `onLoadMore` when it scrolls
 * into the viewport (with configurable rootMargin for pre-fetching).
 * Hides itself while loading to prevent duplicate fetches.
 */
export function InfiniteScrollTrigger({
  hasNextPage,
  loading,
  onLoadMore,
  rootMargin = '200px',
}: InfiniteScrollTriggerProps) {
  const observer = useRef<IntersectionObserver | null>(null);

  const sentinelRef = useCallback(
    (node: HTMLDivElement | null) => {
      if (observer.current) observer.current.disconnect();
      if (!node) return;
      observer.current = new IntersectionObserver(
        (entries) => {
          if (entries[0]?.isIntersecting) {
            onLoadMore();
          }
        },
        { rootMargin },
      );
      observer.current.observe(node);
    },
    [onLoadMore, rootMargin],
  );

  useEffect(() => {
    return () => {
      if (observer.current) observer.current.disconnect();
    };
  }, []);

  if (!hasNextPage || loading) return null;

  return <div ref={sentinelRef} data-testid="scroll-sentinel" className="h-1" />;
}
