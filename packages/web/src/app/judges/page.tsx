import { JudgesList } from './JudgesList';

export default function JudgesPage() {
  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Judges</h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Browse judges across California courts.
      </p>
      <div className="mt-6">
        <JudgesList />
      </div>
    </div>
  );
}
