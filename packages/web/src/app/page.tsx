import Link from 'next/link';

export default function HomePage() {
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-3xl font-bold text-slate-900 dark:text-slate-100">
        Free legal research for everyone
      </h1>
      <p className="mt-4 text-lg text-slate-600 dark:text-slate-400">
        Judgemind captures California tentative rulings and judicial analytics — open source,
        free forever.
      </p>

      <div className="mt-8 flex gap-4">
        <Link
          href="/search"
          className="rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-700"
        >
          Search rulings
        </Link>
        <Link
          href="/rulings"
          className="rounded-lg border border-slate-300 px-5 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          Latest rulings
        </Link>
      </div>
    </div>
  );
}
