import { Suspense } from 'react';
import { PAGE_TITLE } from '@/lib/typography';
import { RulingsFeed } from './RulingsFeed';

export default function RulingsPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className={PAGE_TITLE}>Latest Rulings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Tentative rulings captured today across California courts.
        </p>
      </div>
      <div>
        <Suspense>
          <RulingsFeed />
        </Suspense>
      </div>
    </div>
  );
}
