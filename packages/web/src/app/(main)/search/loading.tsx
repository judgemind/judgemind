import { Skeleton } from '@/components/ui/skeleton';

export default function SearchLoading() {
  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="mt-2 h-4 w-72" />
      </div>
      <Skeleton className="h-10 w-full" />
      <div className="mt-6">
        <Skeleton className="mx-auto h-48 w-full max-w-md rounded-lg" />
      </div>
    </div>
  );
}
