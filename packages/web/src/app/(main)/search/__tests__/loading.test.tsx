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

  it('renders 5 skeleton result placeholders', () => {
    render(<SearchLoading />);
    const skeletons = screen.getAllByTestId('skeleton');
    // Header skeleton (h-8) + subtitle (h-4) + search bar (h-10) + sidebar (h-96) + 5 results (h-32)
    // = 10 skeletons total on desktop
    expect(skeletons.length).toBeGreaterThanOrEqual(9);
  });
});
