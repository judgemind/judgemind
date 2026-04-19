'use client';

import { Button } from '@/components/ui/button';
import { SECTION_HEADING } from '@/lib/typography';
import type { DispatcherCommand, DispatcherRun } from '@/lib/dispatcher-queries';

interface DispatcherControlsProps {
  currentRun: DispatcherRun | null;
  disabled?: boolean;
  onControlClick: (command: DispatcherCommand) => void;
}

/**
 * Five control buttons matching spec §11: Start, Stop (drain), Force-stop,
 * Pause, Resume. Destructive commands (`stop`, `drain`, `force_kill`)
 * trigger a confirmation modal — handled by the parent dashboard.
 */
export function DispatcherControls({
  currentRun,
  disabled,
  onControlClick,
}: DispatcherControlsProps) {
  const daemonActive = currentRun !== null && !currentRun.stoppedAt;

  return (
    <section
      aria-labelledby="dispatcher-controls-heading"
      className="rounded-lg border border-border bg-card p-4"
    >
      <h2 id="dispatcher-controls-heading" className={SECTION_HEADING}>
        Controls
      </h2>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          type="button"
          variant="default"
          size="sm"
          disabled={disabled}
          onClick={() => onControlClick('start')}
        >
          Start
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || !daemonActive}
          onClick={() => onControlClick('pause')}
        >
          Pause
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => onControlClick('resume')}
        >
          Resume
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || !daemonActive}
          onClick={() => onControlClick('drain')}
        >
          Stop (drain)
        </Button>
        <Button
          type="button"
          variant="destructive"
          size="sm"
          disabled={disabled || !daemonActive}
          onClick={() => onControlClick('stop')}
        >
          Force-stop
        </Button>
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        Destructive commands (Stop, Force-stop, Force-kill) require confirmation. Commands
        are written to <code className="font-mono">dispatcher.commands</code> and picked
        up on the daemon&apos;s next tick.
      </p>
    </section>
  );
}
