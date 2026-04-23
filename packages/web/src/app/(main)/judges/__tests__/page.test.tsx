import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../JudgesList', () => ({
  JudgesList: () => <div data-testid="judges-list">List</div>,
}));

import JudgesPage from '../page';

describe('JudgesPage', () => {
  it('renders the heading', () => {
    render(<JudgesPage />);
    expect(screen.getByText('Judges')).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<JudgesPage />);
    expect(
      screen.getByText(/Browse judges across California courts/),
    ).toBeInTheDocument();
  });

  it('renders the JudgesList component', () => {
    render(<JudgesPage />);
    expect(screen.getByTestId('judges-list')).toBeInTheDocument();
  });
});
