'use client';

import { useEffect } from 'react';

export default function ErrorPage({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error('Application error:', error);
  }, [error]);

  return (
    <div className="mx-auto max-w-4xl">
      <div className="rounded-lg border border-slate-200 p-8 text-center dark:border-slate-700">
        <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">
          Something went wrong
        </h1>
        <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
          An unexpected error occurred. Please try again.
        </p>
        <button
          onClick={reset}
          className="mt-4 inline-block rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
