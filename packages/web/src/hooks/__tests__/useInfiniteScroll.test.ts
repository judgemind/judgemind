import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useInfiniteScroll } from '../useInfiniteScroll';

// ---------------------------------------------------------------------------
// IntersectionObserver mock
// ---------------------------------------------------------------------------

let intersectionCallback: IntersectionObserverCallback;
let mockObserve: ReturnType<typeof vi.fn>;
let mockDisconnect: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockObserve = vi.fn();
  mockDisconnect = vi.fn();

  vi.stubGlobal(
    'IntersectionObserver',
    vi.fn((callback: IntersectionObserverCallback) => {
      intersectionCallback = callback;
      return {
        observe: mockObserve,
        disconnect: mockDisconnect,
        unobserve: vi.fn(),
      };
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface MockData {
  items: {
    edges: Array<{ cursor: string; node: { id: string } }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
  };
}

function mergeMockData(prev: MockData, incoming: MockData): MockData {
  return {
    items: {
      ...incoming.items,
      edges: [...prev.items.edges, ...incoming.items.edges],
    },
  };
}

function createDefaultProps(overrides: Partial<Parameters<typeof useInfiniteScroll<MockData>>[0]> = {}) {
  return {
    hasNextPage: true as boolean | undefined,
    endCursor: 'cursor-1' as string | null | undefined,
    loading: false,
    fetchMore: vi.fn().mockResolvedValue({}),
    merge: mergeMockData,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useInfiniteScroll', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns sentinelRef and handleLoadMore', () => {
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps()),
    );

    expect(result.current.sentinelRef).toBeTypeOf('function');
    expect(result.current.handleLoadMore).toBeTypeOf('function');
  });

  it('sets up IntersectionObserver when sentinel ref is attached', () => {
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps()),
    );

    const div = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div);
    });

    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '200px' },
    );
    expect(mockObserve).toHaveBeenCalledWith(div);
  });

  it('uses custom rootMargin', () => {
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ rootMargin: '400px' })),
    );

    const div = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div);
    });

    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '400px' },
    );
  });

  it('calls fetchMore when sentinel becomes visible', () => {
    const fetchMore = vi.fn().mockResolvedValue({});
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore })),
    );

    const div = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div);
    });

    act(() => {
      intersectionCallback(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      );
    });

    expect(fetchMore).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { after: 'cursor-1' },
      }),
    );
  });

  it('does not call fetchMore when sentinel is not intersecting', () => {
    const fetchMore = vi.fn().mockResolvedValue({});
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore })),
    );

    const div = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div);
    });

    act(() => {
      intersectionCallback(
        [{ isIntersecting: false } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      );
    });

    expect(fetchMore).not.toHaveBeenCalled();
  });

  it('does not call fetchMore when loading', () => {
    const fetchMore = vi.fn().mockResolvedValue({});
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore, loading: true })),
    );

    act(() => {
      result.current.handleLoadMore();
    });

    expect(fetchMore).not.toHaveBeenCalled();
  });

  it('does not call fetchMore when endCursor is null', () => {
    const fetchMore = vi.fn().mockResolvedValue({});
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore, endCursor: null })),
    );

    act(() => {
      result.current.handleLoadMore();
    });

    expect(fetchMore).not.toHaveBeenCalled();
  });

  it('does not call fetchMore when endCursor is undefined', () => {
    const fetchMore = vi.fn().mockResolvedValue({});
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore, endCursor: undefined })),
    );

    act(() => {
      result.current.handleLoadMore();
    });

    expect(fetchMore).not.toHaveBeenCalled();
  });

  it('prevents concurrent fetchMore calls via fetchingMore ref', async () => {
    let resolveFirst: () => void;
    const firstPromise = new Promise<void>((resolve) => {
      resolveFirst = resolve;
    });

    const fetchMore = vi.fn().mockReturnValue(firstPromise);
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore })),
    );

    // First call
    act(() => {
      result.current.handleLoadMore();
    });
    expect(fetchMore).toHaveBeenCalledTimes(1);

    // Second call should be blocked (fetchingMore is true)
    act(() => {
      result.current.handleLoadMore();
    });
    expect(fetchMore).toHaveBeenCalledTimes(1);

    // Resolve the first call
    await act(async () => {
      resolveFirst!();
      await firstPromise;
    });

    // Now the third call should succeed
    act(() => {
      result.current.handleLoadMore();
    });
    expect(fetchMore).toHaveBeenCalledTimes(2);
  });

  it('disconnects observer on unmount', () => {
    const { result, unmount } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps()),
    );

    const div = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div);
    });

    unmount();
    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('disconnects previous observer when sentinel ref changes', () => {
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps()),
    );

    const div1 = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div1);
    });
    expect(mockObserve).toHaveBeenCalledTimes(1);

    const div2 = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div2);
    });

    // Previous observer should have been disconnected
    expect(mockDisconnect).toHaveBeenCalled();
    expect(mockObserve).toHaveBeenCalledTimes(2);
  });

  it('disconnects observer when sentinel ref is set to null', () => {
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps()),
    );

    const div = document.createElement('div');
    act(() => {
      result.current.sentinelRef(div);
    });

    act(() => {
      result.current.sentinelRef(null);
    });

    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('passes merge function to updateQuery', () => {
    const merge = vi.fn().mockReturnValue({ items: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } } });
    const fetchMore = vi.fn().mockResolvedValue({});
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore, merge })),
    );

    act(() => {
      result.current.handleLoadMore();
    });

    // Get the updateQuery function that was passed to fetchMore
    const updateQuery = fetchMore.mock.calls[0][0].updateQuery;
    const prev = { items: { edges: [{ cursor: 'c1', node: { id: '1' } }], pageInfo: { hasNextPage: true, endCursor: 'c1' } } };
    const fetchMoreResult = { items: { edges: [{ cursor: 'c2', node: { id: '2' } }], pageInfo: { hasNextPage: false, endCursor: null } } };

    updateQuery(prev, { fetchMoreResult });
    expect(merge).toHaveBeenCalledWith(prev, fetchMoreResult);
  });

  it('returns prev when fetchMoreResult is falsy', () => {
    const fetchMore = vi.fn().mockResolvedValue({});
    const merge = vi.fn();
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore, merge })),
    );

    act(() => {
      result.current.handleLoadMore();
    });

    const updateQuery = fetchMore.mock.calls[0][0].updateQuery;
    const prev = { items: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } } };

    // Simulate falsy fetchMoreResult
    const returned = updateQuery(prev, { fetchMoreResult: null });
    expect(returned).toBe(prev);
    expect(merge).not.toHaveBeenCalled();
  });

  it('discards stale results when filterDeps change mid-flight', async () => {
    let resolvePromise: () => void;
    const fetchPromise = new Promise<void>((resolve) => {
      resolvePromise = resolve;
    });

    const fetchMore = vi.fn().mockReturnValue(fetchPromise);
    const merge = vi.fn();

    const { result, rerender } = renderHook(
      ({ filterDeps }) =>
        useInfiniteScroll<MockData>(
          createDefaultProps({ fetchMore, merge, filterDeps }),
        ),
      { initialProps: { filterDeps: ['county-A'] } },
    );

    // Start a fetchMore call
    act(() => {
      result.current.handleLoadMore();
    });
    expect(fetchMore).toHaveBeenCalledTimes(1);

    // Get the updateQuery that was passed
    const updateQuery = fetchMore.mock.calls[0][0].updateQuery;

    // Change the filter deps — this increments the generation counter
    rerender({ filterDeps: ['county-B'] });

    // Now call updateQuery with the stale result — it should discard
    const prev = { items: { edges: [], pageInfo: { hasNextPage: true, endCursor: 'c1' } } };
    const fetchMoreResult = { items: { edges: [{ cursor: 'c2', node: { id: '2' } }], pageInfo: { hasNextPage: false, endCursor: null } } };

    const returned = updateQuery(prev, { fetchMoreResult });
    expect(returned).toBe(prev); // Should return prev, not merged
    expect(merge).not.toHaveBeenCalled();

    // Clean up the promise
    await act(async () => {
      resolvePromise!();
      await fetchPromise;
    });
  });

  it('does not discard results when filterDeps is not provided', () => {
    const fetchMore = vi.fn().mockResolvedValue({});
    const merge = vi.fn().mockReturnValue({ items: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } } });
    const { result } = renderHook(() =>
      useInfiniteScroll<MockData>(createDefaultProps({ fetchMore, merge })),
    );

    act(() => {
      result.current.handleLoadMore();
    });

    const updateQuery = fetchMore.mock.calls[0][0].updateQuery;
    const prev = { items: { edges: [], pageInfo: { hasNextPage: true, endCursor: 'c1' } } };
    const fetchMoreResult = { items: { edges: [{ cursor: 'c2', node: { id: '2' } }], pageInfo: { hasNextPage: false, endCursor: null } } };

    updateQuery(prev, { fetchMoreResult });
    expect(merge).toHaveBeenCalledWith(prev, fetchMoreResult);
  });
});
