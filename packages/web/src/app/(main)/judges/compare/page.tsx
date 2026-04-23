import { Suspense } from 'react';
import Link from 'next/link';
import { PAGE_TITLE } from '@/lib/typography';
import { JudgeComparison } from './JudgeComparison';

export default function JudgeComparePage() {
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      {/* Breadcrumb */}
      <nav className="text-sm text-muted-foreground" aria-label="Breadcrumb">
        <Link href="/judges" className="hover:text-primary">
          Judges
        </Link>
        <span className="mx-2">/</span>
        <span className="text-foreground">Compare</span>
      </nav>

      <div>
        <h1 className={PAGE_TITLE}>Compare Judges</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Side-by-side comparison of judge analytics across courts.
        </p>
      </div>

      <Suspense>
        <JudgeComparison />
      </Suspense>
    </div>
  );
}
