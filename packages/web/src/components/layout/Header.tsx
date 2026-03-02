'use client';

import Link from 'next/link';
import { useTheme } from '@/providers/ThemeProvider';

export function Header() {
  const { theme, toggle } = useTheme();

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

      <button
        onClick={toggle}
        aria-label="Toggle dark mode"
        className="rounded-md p-2 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
      >
        {theme === 'dark' ? '☀️' : '🌙'}
      </button>
    </header>
  );
}
