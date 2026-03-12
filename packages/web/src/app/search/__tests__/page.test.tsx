import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../SearchPage', () => ({
  SearchPage: () => <div data-testid="search-page">Search component</div>,
}));

import SearchRoute from '../page';

describe('SearchRoute (SSR wrapper)', () => {
  it('renders the SearchPage component inside Suspense', () => {
    render(<SearchRoute />);
    expect(screen.getByTestId('search-page')).toBeInTheDocument();
  });

  it('renders without crashing', () => {
    const { container } = render(<SearchRoute />);
    expect(container).toBeTruthy();
  });
});
