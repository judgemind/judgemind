import { CasesList } from './CasesList';

export default function CasesPage() {
  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Cases</h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Browse court cases across California.
      </p>
      <div className="mt-6">
        <CasesList />
      </div>
    </div>
  );
}
