import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { InfiniteScrollTrigger } from '../InfiniteScrollTrigger';

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

describe('InfiniteScrollTrigger', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders sentinel when hasNextPage is true and not loading', () => {
    render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={vi.fn()}
      />,
    );
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
  });

  it('does not render sentinel when hasNextPage is false', () => {
    render(
      <InfiniteScrollTrigger
        hasNextPage={false}
        loading={false}
        onLoadMore={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('does not render sentinel while loading', () => {
    render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={true}
        onLoadMore={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('sets up IntersectionObserver with default rootMargin', () => {
    render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={vi.fn()}
      />,
    );
    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '200px' },
    );
    expect(mockObserve).toHaveBeenCalled();
  });

  it('sets up IntersectionObserver with custom rootMargin', () => {
    render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={vi.fn()}
        rootMargin="400px"
      />,
    );
    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '400px' },
    );
  });

  it('calls onLoadMore when sentinel becomes visible', () => {
    const onLoadMore = vi.fn();
    render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={onLoadMore}
      />,
    );

    intersectionCallback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it('does not call onLoadMore when sentinel is not intersecting', () => {
    const onLoadMore = vi.fn();
    render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={onLoadMore}
      />,
    );

    intersectionCallback(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(onLoadMore).not.toHaveBeenCalled();
  });

  it('disconnects observer on unmount', () => {
    const { unmount } = render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={vi.fn()}
      />,
    );
    unmount();
    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('disconnects previous observer when ref changes', () => {
    const { rerender } = render(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={vi.fn()}
      />,
    );

    // First render: observer connected
    expect(mockObserve).toHaveBeenCalledTimes(1);

    // Re-render with new onLoadMore (triggers new sentinelRef callback)
    const newOnLoadMore = vi.fn();
    rerender(
      <InfiniteScrollTrigger
        hasNextPage={true}
        loading={false}
        onLoadMore={newOnLoadMore}
      />,
    );

    // Previous observer should have been disconnected
    expect(mockDisconnect).toHaveBeenCalled();
  });
});
