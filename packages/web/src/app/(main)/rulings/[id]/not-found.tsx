import Link from 'next/link';

export default function RulingNotFound() {
  return (
    <div className="mx-auto max-w-4xl">
      <div className="rounded-lg border border-border p-8 text-center">
        <h1 className="text-xl font-bold text-foreground">
          Ruling Not Found
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          The ruling you are looking for does not exist or has not been captured yet.
        </p>
        <Link
          href="/rulings"
          className="mt-4 inline-block rounded-lg bg-brand-accent px-4 py-2 text-sm font-medium text-white hover:bg-brand-accent-hover"
        >
          Back to Rulings
        </Link>
      </div>
    </div>
  );
}
