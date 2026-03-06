import { RulingsFeed } from './RulingsFeed';

export default function RulingsPage() {
  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Latest Rulings</h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Tentative rulings captured today across California courts.
      </p>
      <div className="mt-6">
        <RulingsFeed />
      </div>
    </div>
  );
}
