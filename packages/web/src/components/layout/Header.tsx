'use client';

import { useCallback, useState } from 'react';
import Link from 'next/link';
import { useTheme } from '@/providers/ThemeProvider';
import { useAuth } from '@/providers/AuthProvider';
import { Sidebar } from '@/components/layout/Sidebar';

export function Header() {
  const { theme, toggle } = useTheme();
  const { user, loading, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  const closeMenu = useCallback(() => setMenuOpen(false), []);

  return (
    <>
      <header className="sticky top-0 z-50 flex h-14 items-center border-b border-slate-200 bg-white px-4 dark:border-slate-700 dark:bg-slate-900">
        {/* Hamburger — visible only on mobile (below lg breakpoint) */}
        <button
          onClick={() => setMenuOpen(true)}
          aria-label="Toggle menu"
          className="mr-2 rounded-md p-2 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800 lg:hidden"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            className="h-5 w-5"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>

        <Link href="/" className="mr-8 text-lg font-semibold text-brand-700 dark:text-brand-500">
          Judgemind
        </Link>

        <nav className="flex flex-1 items-center gap-6 text-sm font-medium">
          <Link
            href="/search"
            className="text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
          >
            Search
          </Link>
          <Link
            href="/rulings"
            className="text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
          >
            Rulings
          </Link>
        </nav>

        <div className="flex items-center gap-2">
          <button
            onClick={toggle}
            aria-label="Toggle dark mode"
            className="rounded-md p-2 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
          >
            {theme === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19'}
          </button>

          {!loading && (
            <>
              {user ? (
                <button
                  onClick={() => void logout()}
                  className="rounded-md px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
                >
                  Log out
                </button>
              ) : (
                <Link
                  href="/auth/login"
                  className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700"
                >
                  Log in
                </Link>
              )}
            </>
          )}
        </div>
      </header>

      {/* Mobile slide-out menu */}
      {menuOpen && (
        <div role="dialog" aria-modal="true" className="fixed inset-0 z-[60] lg:hidden">
          {/* Backdrop */}
          <div
            data-testid="mobile-menu-backdrop"
            className="absolute inset-0 bg-black/40"
            onClick={closeMenu}
          />

          {/* Slide-out panel */}
          <aside className="absolute inset-y-0 left-0 w-64 border-r border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900">
            <div className="flex h-14 items-center justify-between border-b border-slate-200 px-4 dark:border-slate-700">
              <span className="text-lg font-semibold text-brand-700 dark:text-brand-500">
                Judgemind
              </span>
              <button
                onClick={closeMenu}
                aria-label="Close menu"
                className="rounded-md p-2 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  className="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2}
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <Sidebar onLinkClick={closeMenu} />
          </aside>
        </div>
      )}
    </>
  );
}
