import { Suspense } from 'react';
import { SearchPage } from './SearchPage';
import { Skeleton } from '@/components/ui/skeleton';

export default function SearchRoute() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-6">
          <div>
            <Skeleton className="h-8 w-48" />
            <Skeleton className="mt-2 h-4 w-72" />
          </div>
          <Skeleton className="h-10 w-full" />
          <div>
            <Skeleton className="mx-auto h-48 w-full max-w-md rounded-lg" />
          </div>
        </div>
      }
    >
      <SearchPage />
    </Suspense>
  );
}
