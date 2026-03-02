export default function RulingsPage() {
  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Latest Rulings</h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Tentative rulings captured today across California courts.
      </p>
      <div className="mt-6 rounded-lg border border-slate-200 dark:border-slate-700">
        <p className="p-8 text-center text-slate-400 dark:text-slate-500">
          No rulings available — scrapers not yet running.
        </p>
      </div>
    </div>
  );
}
