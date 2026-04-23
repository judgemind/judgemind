import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../JudgeComparison', () => ({
  JudgeComparison: () => <div data-testid="judge-comparison">Comparison</div>,
}));

vi.mock('next/link', () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => (
    <a href={href}>
      {children}
    </a>
  ),
}));

import JudgeComparePage from '../page';

describe('JudgeComparePage', () => {
  it('renders the heading', () => {
    render(<JudgeComparePage />);
    expect(screen.getByText('Compare Judges')).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<JudgeComparePage />);
    expect(
      screen.getByText(/Side-by-side comparison of judge analytics/),
    ).toBeInTheDocument();
  });

  it('renders breadcrumb with link to judges list', () => {
    render(<JudgeComparePage />);
    const link = screen.getByText('Judges');
    expect(link.closest('a')).toHaveAttribute('href', '/judges');
  });

  it('renders the JudgeComparison component', () => {
    render(<JudgeComparePage />);
    expect(screen.getByTestId('judge-comparison')).toBeInTheDocument();
  });
});
