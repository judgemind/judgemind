import Link from 'next/link';
import { Button } from '@/components/ui/button';

export default function CaseNotFound() {
  return (
    <div className="mx-auto max-w-4xl">
      <div className="rounded-lg border border-border p-8 text-center">
        <h1 className="text-xl font-bold text-foreground">
          Case Not Found
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          The case you are looking for does not exist or has not been captured yet.
        </p>
        <div className="mt-4">
          <Button asChild variant="outline">
            <Link href="/cases">Back to Cases</Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
