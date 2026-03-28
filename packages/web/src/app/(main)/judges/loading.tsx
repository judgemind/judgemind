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
        <Skeleton className="h-4 w-48" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-32" />
      </TableCell>
      <TableCell className="text-right">
        <Skeleton className="ml-auto h-4 w-12" />
      </TableCell>
    </TableRow>
  );
}

export default function JudgesLoading() {
  return (
    <div className="mx-auto max-w-4xl">
      <Skeleton className="h-8 w-32" />
      <Skeleton className="mt-1 h-4 w-64" />
      <div className="mt-6">
        <div className="mb-4 flex gap-3">
          <Skeleton className="h-10 w-48" />
          <Skeleton className="h-10 w-[180px]" />
        </div>
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>County</TableHead>
                <TableHead className="text-right">Rulings</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Array.from({ length: 10 }).map((_, i) => (
                <SkeletonRow key={i} />
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}
