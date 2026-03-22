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

  it('renders skeleton result row placeholders inside bordered container', () => {
    const { container } = render(<SearchLoading />);
    const skeletons = screen.getAllByTestId('skeleton');
    // Header (h-8) + subtitle (h-4) + search bar (h-10) + sidebar (h-96) = 4 chrome skeletons
    // + 5 result rows x 7 skeletons each (title, subtitle, date, 2 badges, 2 excerpt lines) = 35
    // = 39 total
    expect(skeletons.length).toBeGreaterThanOrEqual(30);
    // Verify the bordered container pattern is used
    const borderedContainer = container.querySelector('.divide-y.rounded-lg.border');
    expect(borderedContainer).toBeTruthy();
  });
});
