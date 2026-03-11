'use client';

import './globals.css';

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Replicate theme detection from layout.tsx to avoid flash */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              try {
                var t = localStorage.getItem('theme');
                var p = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
                if ((t || p) === 'dark') document.documentElement.classList.add('dark');
              } catch(e) {}
            `,
          }}
        />
      </head>
      <body className="bg-white text-slate-900 dark:bg-slate-900 dark:text-slate-50">
        <div className="flex min-h-screen items-center justify-center">
          <div className="mx-auto max-w-4xl">
            <div className="rounded-lg border border-slate-200 p-8 text-center dark:border-slate-700">
              <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">
                Something went wrong
              </h1>
              <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                A critical error occurred. Please try again.
              </p>
              <button
                onClick={reset}
                className="mt-4 inline-block rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700"
              >
                Try again
              </button>
            </div>
          </div>
        </div>
      </body>
    </html>
  );
}
