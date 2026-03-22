import { Skeleton } from '@/components/ui/skeleton';

export default function SearchLoading() {
  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="mt-2 h-4 w-72" />
      </div>
      <Skeleton className="h-10 w-full" />
      <div className="mt-6 flex gap-6">
        <div className="hidden w-64 shrink-0 lg:block">
          <Skeleton className="h-96 w-full rounded-lg" />
        </div>
        <div className="flex-1 space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full rounded-lg" />
          ))}
        </div>
      </div>
    </div>
  );
}
