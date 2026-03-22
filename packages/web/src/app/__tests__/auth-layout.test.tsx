import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import AuthLayout from '../(auth)/layout';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AuthLayout', () => {
  it('renders children', () => {
    render(<AuthLayout>Login form here</AuthLayout>);
    expect(screen.getByText('Login form here')).toBeInTheDocument();
  });

  it('does not render a sidebar', () => {
    render(<AuthLayout>Content</AuthLayout>);
    expect(screen.queryByRole('navigation', { name: 'Sidebar' })).not.toBeInTheDocument();
  });

  it('wraps content in a main element with id main-content', () => {
    render(<AuthLayout>Auth content</AuthLayout>);
    const main = screen.getByRole('main');
    expect(main).toBeInTheDocument();
    expect(main).toHaveTextContent('Auth content');
    expect(main).toHaveAttribute('id', 'main-content');
  });

  it('centers content with flex layout', () => {
    render(<AuthLayout>Content</AuthLayout>);
    const main = screen.getByRole('main');
    expect(main).toHaveClass('flex', 'flex-1', 'items-center', 'justify-center');
  });
});
