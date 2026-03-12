import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../CasesList', () => ({
  CasesList: () => <div data-testid="cases-list">List</div>,
}));

import CasesPage from '../page';

describe('CasesPage', () => {
  it('renders the heading', () => {
    render(<CasesPage />);
    expect(screen.getByText('Cases')).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<CasesPage />);
    expect(
      screen.getByText(/Browse court cases across California/),
    ).toBeInTheDocument();
  });

  it('renders the CasesList component', () => {
    render(<CasesPage />);
    expect(screen.getByTestId('cases-list')).toBeInTheDocument();
  });
});
