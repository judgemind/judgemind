import { formatOutcome, getOutcomeBadgeVariant, getOutcomeBadgeListClass } from '@/lib/display-helpers';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

interface OutcomeBadgeProps {
  /** The outcome code (e.g. "granted", "denied", "granted_in_part", null). */
  outcome: string | null;
  /** Additional CSS classes to merge with the badge styling. */
  className?: string;
}

/**
 * Shared outcome badge component used across all surfaces that display ruling outcomes.
 * Renders a pill-shaped badge with semantic colors per BRAND.md:
 * - Green 700 for granted outcomes (including granted_in_part)
 * - Red 700 for denied outcomes (including denied_in_part)
 * - Stone 500 for neutral outcomes (moot, continued, off_calendar, other)
 */
export function OutcomeBadge({ outcome, className }: OutcomeBadgeProps) {
  return (
    <Badge
      variant={getOutcomeBadgeVariant(outcome)}
      className={cn(getOutcomeBadgeListClass(outcome), className)}
    >
      {formatOutcome(outcome)}
    </Badge>
  );
}
