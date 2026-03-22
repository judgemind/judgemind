import { Suspense } from 'react';
import { PAGE_TITLE } from '@/lib/typography';
import { JudgesList } from './JudgesList';

export default function JudgesPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className={PAGE_TITLE}>Judges</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Browse judges across California courts.
        </p>
      </div>
      <div>
        <Suspense>
          <JudgesList />
        </Suspense>
      </div>
    </div>
  );
}
