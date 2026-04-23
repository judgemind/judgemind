import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../RulingsFeed', () => ({
  RulingsFeed: () => <div data-testid="rulings-feed">Feed</div>,
}));

import RulingsPage from '../page';

describe('RulingsPage', () => {
  it('renders the heading', () => {
    render(<RulingsPage />);
    expect(screen.getByText('Latest Rulings')).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<RulingsPage />);
    expect(
      screen.getByText(/Tentative rulings captured today/),
    ).toBeInTheDocument();
  });

  it('renders the RulingsFeed component', () => {
    render(<RulingsPage />);
    expect(screen.getByTestId('rulings-feed')).toBeInTheDocument();
  });
});
