export default function SearchPage() {
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Search Rulings</h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Full-text search across California tentative rulings.
      </p>
      <div className="mt-6">
        <input
          type="search"
          placeholder="Search by keyword, case number, judge, or party…"
          className="w-full rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
        />
      </div>
    </div>
  );
}
