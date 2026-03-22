import Link from 'next/link';

export default function HomePage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-foreground">
          Free legal research for everyone
        </h1>
        <p className="mt-4 text-lg text-muted-foreground">
          Judgemind captures California tentative rulings and judicial analytics — open source,
          free forever.
        </p>
      </div>

      <div className="flex gap-3">
        <Link
          href="/search"
          className="rounded-lg bg-brand-accent px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          Search rulings
        </Link>
        <Link
          href="/rulings"
          className="rounded-lg border border-slate-300 px-5 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          Latest rulings
        </Link>
      </div>
    </div>
  );
}
