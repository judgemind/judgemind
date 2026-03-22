import { Suspense } from 'react';
import { PAGE_TITLE } from '@/lib/typography';
import { CasesList } from './CasesList';

export default function CasesPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className={PAGE_TITLE}>Cases</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Browse court cases across California.
        </p>
      </div>
      <div>
        <Suspense>
          <CasesList />
        </Suspense>
      </div>
    </div>
  );
}
