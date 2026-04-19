'use client';

import { SECTION_HEADING } from '@/lib/typography';

interface QueuePanelProps {
  queueDepth: number;
}

/**
 * Queue panel. In Phase 1 the GraphQL resolver returns queueDepth = 0
 * until the daemon's queue-scan lands (see sub-task C follow-up), so this
 * is a minimal summary card. Phase 2 will expand to a full per-issue
 * listing with blocked-by counts.
 */
export function QueuePanel({ queueDepth }: QueuePanelProps) {
  return (
    <section
      aria-labelledby="queue-heading"
      className="rounded-lg border border-border bg-card p-4"
    >
      <h2 id="queue-heading" className={SECTION_HEADING}>
        Queue
      </h2>
      <div className="mt-3">
        <p className="text-sm text-foreground">
          <span className="font-semibold">{queueDepth}</span>{' '}
          <span className="text-muted-foreground">
            {queueDepth === 1 ? 'issue' : 'issues'} ready for pickup
          </span>
        </p>
        <p className="mt-2 text-xs text-muted-foreground">
          Per-issue listing with blocked-by counts lands with the daemon queue-scan
          follow-up.
        </p>
      </div>
    </section>
  );
}
