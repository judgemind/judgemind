import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: ({ className }: { className?: string }) => (
    <div data-testid="skeleton" className={className} />
  ),
}));

import RulingsLoading from '../loading';

describe('RulingsLoading', () => {
  it('renders without crashing', () => {
    const { container } = render(<RulingsLoading />);
    expect(container).toBeTruthy();
  });

  it('renders skeleton elements', () => {
    render(<RulingsLoading />);
    const skeletons = screen.getAllByTestId('skeleton');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders 8 skeleton rows inside a bordered container', () => {
    const { container } = render(<RulingsLoading />);
    const borderedContainer = container.querySelector('.divide-y.rounded-lg.border');
    expect(borderedContainer).toBeTruthy();
    // Each skeleton row is a direct child div with px-4 py-3
    const rows = borderedContainer?.querySelectorAll(':scope > div');
    expect(rows).toHaveLength(8);
  });
});
