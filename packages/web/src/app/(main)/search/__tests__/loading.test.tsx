import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: ({ className }: { className?: string }) => (
    <div data-testid="skeleton" className={className} />
  ),
}));

import SearchLoading from '../loading';

describe('SearchLoading', () => {
  it('renders without crashing', () => {
    const { container } = render(<SearchLoading />);
    expect(container).toBeTruthy();
  });

  it('renders skeleton elements', () => {
    render(<SearchLoading />);
    const skeletons = screen.getAllByTestId('skeleton');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders skeleton placeholders for header, search bar, and empty state', () => {
    render(<SearchLoading />);
    const skeletons = screen.getAllByTestId('skeleton');
    // Header (h-8) + subtitle (h-4) + search bar (h-10) + empty state placeholder (h-48) = 4
    expect(skeletons.length).toBeGreaterThanOrEqual(4);
  });
});
