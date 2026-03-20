import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';

export default function JudgeNotFound() {
  return (
    <div className="mx-auto max-w-4xl">
      <Card className="border-dashed">
        <CardContent className="py-8 text-center">
          <h1 className="text-xl font-bold text-foreground">
            Judge Not Found
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            The judge you are looking for does not exist or has not been captured yet.
          </p>
          <div className="mt-4">
            <Button asChild>
              <Link href="/">Back to Home</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
