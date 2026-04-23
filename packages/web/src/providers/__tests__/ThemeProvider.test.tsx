import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';

import { ThemeProvider, useTheme } from '../ThemeProvider';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function ThemeConsumer() {
  const { theme, toggle } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggle}>Toggle</button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ThemeProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    document.documentElement.classList.remove('dark');
    document.documentElement.style.colorScheme = '';
    // Default: prefers light
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  });

  it('renders children', () => {
    render(
      <ThemeProvider>
        <span>Hello</span>
      </ThemeProvider>,
    );
    expect(screen.getByText('Hello')).toBeInTheDocument();
  });

  it('defaults to light theme', () => {
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );
    // Initial state before useEffect runs
    expect(screen.getByTestId('theme').textContent).toBe('light');
  });

  it('reads stored theme from localStorage', async () => {
    localStorage.setItem('theme', 'dark');
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );
    // After useEffect, theme should be dark
    await vi.waitFor(() => {
      expect(screen.getByTestId('theme').textContent).toBe('dark');
    });
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe('dark');
  });

  it('respects prefers-color-scheme: dark when no stored theme', async () => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: query === '(prefers-color-scheme: dark)',
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    await vi.waitFor(() => {
      expect(screen.getByTestId('theme').textContent).toBe('dark');
    });
  });

  it('toggles from light to dark', async () => {
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    act(() => {
      screen.getByText('Toggle').click();
    });

    expect(screen.getByTestId('theme').textContent).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe('dark');
    expect(localStorage.getItem('theme')).toBe('dark');
  });

  it('toggles from dark back to light', async () => {
    localStorage.setItem('theme', 'dark');
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    await vi.waitFor(() => {
      expect(screen.getByTestId('theme').textContent).toBe('dark');
    });

    act(() => {
      screen.getByText('Toggle').click();
    });

    expect(screen.getByTestId('theme').textContent).toBe('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe('light');
    expect(localStorage.getItem('theme')).toBe('light');
  });

  it('sets color-scheme to light on initial render when no stored theme', async () => {
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );
    await vi.waitFor(() => {
      expect(document.documentElement.style.colorScheme).toBe('light');
    });
  });
});

describe('useTheme', () => {
  it('returns default values outside of provider', () => {
    function Consumer() {
      const { theme } = useTheme();
      return <span data-testid="theme">{theme}</span>;
    }
    render(<Consumer />);
    expect(screen.getByTestId('theme').textContent).toBe('light');
  });
});
