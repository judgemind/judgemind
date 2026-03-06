import { Suspense } from 'react';
import { SearchPage } from './SearchPage';

export default function SearchRoute() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-5xl">
          <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">
            Search Rulings
          </h1>
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
            Full-text search across California tentative rulings.
          </p>
          <div className="mt-6">
            <div className="h-11 w-full animate-pulse rounded-lg bg-slate-200 dark:bg-slate-700" />
          </div>
        </div>
      }
    >
      <SearchPage />
    </Suspense>
  );
}
