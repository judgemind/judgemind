import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: ({ className }: { className?: string }) => (
    <div data-testid="skeleton" className={className} />
  ),
}));

import CasesLoading from '../loading';

describe('CasesLoading', () => {
  it('renders without crashing', () => {
    const { container } = render(<CasesLoading />);
    expect(container).toBeTruthy();
  });

  it('renders skeleton elements for the list layout', () => {
    render(<CasesLoading />);
    const skeletons = screen.getAllByTestId('skeleton');
    // Title, subtitle, 5 filter skeletons, plus 8 rows * 4 skeletons each = 39
    expect(skeletons.length).toBeGreaterThan(8);
  });

  it('uses divide-y row layout (not table)', () => {
    const { container } = render(<CasesLoading />);
    expect(container.querySelector('table')).not.toBeInTheDocument();
    expect(container.querySelector('.divide-y')).toBeInTheDocument();
  });
});
