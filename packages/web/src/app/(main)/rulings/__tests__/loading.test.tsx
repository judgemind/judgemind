import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: ({ className }: { className?: string }) => (
    <div data-testid="skeleton" className={className} />
  ),
}));

vi.mock('@/components/ui/card', () => ({
  Card: ({ children, ...props }: { children: React.ReactNode }) => (
    <div data-testid="card" {...props}>{children}</div>
  ),
  CardContent: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="card-content" className={className}>{children}</div>
  ),
}));

import RulingsLoading from '../loading';

describe('RulingsLoading', () => {
  it('renders without crashing', () => {
    const { container } = render(<RulingsLoading />);
    expect(container).toBeTruthy();
  });

  it('renders skeleton cards', () => {
    render(<RulingsLoading />);
    const skeletons = screen.getAllByTestId('skeleton');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders 8 skeleton cards', () => {
    render(<RulingsLoading />);
    const cards = screen.getAllByTestId('card');
    expect(cards).toHaveLength(8);
  });
});
