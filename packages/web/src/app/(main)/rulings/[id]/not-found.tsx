import Link from 'next/link';
import { Button } from '@/components/ui/button';

export default function RulingNotFound() {
  return (
    <div className="mx-auto max-w-4xl">
      <div className="rounded-lg border border-border p-8 text-center">
        <h1 className="text-xl font-bold text-foreground">
          Ruling Not Found
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          The ruling you are looking for does not exist or has not been captured yet.
        </p>
        <div className="mt-4">
          <Button asChild variant="outline">
            <Link href="/rulings">Back to Rulings</Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
