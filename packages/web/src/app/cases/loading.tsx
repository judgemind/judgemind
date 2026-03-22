import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

function SkeletonRow() {
  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col gap-1">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-3 w-48" />
        </div>
      </TableCell>
      <TableCell>
        <Skeleton className="h-3 w-24" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-3 w-16" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-5 w-14" />
      </TableCell>
    </TableRow>
  );
}

export default function CasesLoading() {
  return (
    <div className="mx-auto max-w-4xl">
      <Skeleton className="h-8 w-24" />
      <Skeleton className="mt-2 h-4 w-64" />
      <div className="mt-6">
        <div className="mb-4 flex flex-wrap gap-3">
          <Skeleton className="h-10 w-48" />
          <Skeleton className="h-10 w-32" />
          <Skeleton className="h-10 w-32" />
        </div>
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Case</TableHead>
                <TableHead>Court</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Array.from({ length: 8 }).map((_, i) => (
                <SkeletonRow key={i} />
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}
