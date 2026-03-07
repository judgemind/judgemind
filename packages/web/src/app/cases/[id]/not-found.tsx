import Link from 'next/link';

export default function CaseNotFound() {
  return (
    <div className="mx-auto max-w-4xl">
      <div className="rounded-lg border border-slate-200 p-8 text-center dark:border-slate-700">
        <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">
          Case Not Found
        </h1>
        <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
          The case you are looking for does not exist or has not been captured yet.
        </p>
        <Link
          href="/"
          className="mt-4 inline-block rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700"
        >
          Back to Home
        </Link>
      </div>
    </div>
  );
}
