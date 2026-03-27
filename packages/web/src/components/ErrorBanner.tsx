import { AlertCircle } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

interface ErrorBannerProps {
  /** The error message to display. */
  message: string;
  /** Callback invoked when the user clicks "Try again". Defaults to reloading the page. */
  onRetry?: () => void;
  /** Additional CSS classes to merge with the card wrapper. */
  className?: string;
}

/**
 * Page-level error banner used for full-section error states.
 * Renders a Card with soft styling (bg-muted, text-red-700), an AlertCircle icon,
 * the error message, and a "Try again" button.
 *
 * Use this for error states that replace an entire section (e.g., failed data load).
 * For smaller inline errors (e.g., document viewer failures), use `InlineError` instead.
 */
export function ErrorBanner({ message, onRetry, className }: ErrorBannerProps) {
  return (
    <Card className={cn('border bg-muted', className)}>
      <CardContent className="flex flex-col items-center justify-center py-8">
        <AlertCircle className="mb-3 h-8 w-8 text-red-700/70" aria-hidden="true" />
        <p className="text-sm text-red-700">
          {message}
        </p>
        <button
          type="button"
          onClick={onRetry ?? (() => window.location.reload())}
          className="mt-3 text-sm font-medium text-muted-foreground underline underline-offset-2 hover:text-foreground"
        >
          Try again
        </button>
      </CardContent>
    </Card>
  );
}

interface InlineErrorProps {
  /** The error message to display. */
  message: string;
  /** Callback invoked when the user clicks "Try again". Defaults to reloading the page. */
  onRetry?: () => void;
  /** Additional CSS classes to merge with the wrapper div. */
  className?: string;
  /** Optional data-testid attribute for testing. */
  testId?: string;
}

/**
 * Inline error indicator for use inside cards, viewers, or other contained areas.
 * Renders a compact horizontal layout with a small AlertCircle icon, the error message,
 * and a "Try again" button.
 *
 * Use this for error states within a larger component (e.g., document viewer failures).
 * For full-section error states, use `ErrorBanner` instead.
 */
export function InlineError({ message, onRetry, className, testId }: InlineErrorProps) {
  return (
    <div className={cn(className)} {...(testId ? { 'data-testid': testId } : {})}>
      <div className="flex items-center gap-2">
        <AlertCircle className="h-4 w-4 shrink-0 text-red-700/70" aria-hidden="true" />
        <p className="text-sm text-red-700">
          {message}
        </p>
      </div>
      <button
        type="button"
        onClick={onRetry ?? (() => window.location.reload())}
        className="mt-2 text-sm font-medium text-muted-foreground underline underline-offset-2 hover:text-foreground"
      >
        Try again
      </button>
    </div>
  );
}
