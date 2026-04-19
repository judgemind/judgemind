'use client';

import { Button } from '@/components/ui/button';
import type { DispatcherCommand, DispatcherRun } from '@/lib/dispatcher-queries';

interface DispatcherControlsProps {
  currentRun: DispatcherRun | null;
  disabled?: boolean;
  onControlClick: (command: DispatcherCommand) => void;
}

/**
 * Five control buttons matching spec §11: Start · Pause · Resume ·
 * Stop (drain) · Force-stop.
 *
 * Rendered as a flat inline cluster — no card wrapper, no explanatory
 * paragraph — per #2805 §1.1. Same size/variant/enabled rules as the
 * previous implementation so behaviour is unchanged.
 *
 * The implementation-trivia footnote ("commands written to
 * dispatcher.commands") is intentionally removed — the operator has
 * internalized it and the page is about visibility, not tutorials.
 *
 * Daemon-side command handlers are tracked in #2801 — until that lands
 * some click handlers write a `dispatcher.commands` row but produce no
 * observable daemon behaviour change. This UI ships the correct writes
 * today.
 */
export function DispatcherControls({
  currentRun,
  disabled,
  onControlClick,
}: DispatcherControlsProps) {
  const daemonActive = currentRun !== null && !currentRun.stoppedAt;

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="dispatcher-controls">
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
  );
}
