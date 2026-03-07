'use client';

import Link from 'next/link';
import { useTheme } from '@/providers/ThemeProvider';
import { useAuth } from '@/providers/AuthProvider';

export function Header() {
  const { theme, toggle } = useTheme();
  const { user, loading, logout } = useAuth();

  return (
    <header className="sticky top-0 z-50 flex h-14 items-center border-b border-slate-200 bg-white px-4 dark:border-slate-700 dark:bg-slate-900">
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
  );
}
