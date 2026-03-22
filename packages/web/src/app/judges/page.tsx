import { Suspense } from 'react';
import { JudgesList } from './JudgesList';

export default function JudgesPage() {
  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold tracking-tight text-foreground">Judges</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Browse judges across California courts.
      </p>
      <div className="mt-6">
        <Suspense>
          <JudgesList />
        </Suspense>
      </div>
    </div>
  );
}
