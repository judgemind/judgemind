'use client';

import { Button } from '@/components/ui/button';
import type { DispatcherCommand, DispatcherRun } from '@/lib/dispatcher-queries';

interface DispatcherControlsProps {
  currentRun: DispatcherRun | null;
  disabled?: boolean;
  onControlClick: (command: DispatcherCommand) => void;
}

/**
 * Three control buttons — Start, Stop, Force Stop — matching the
 * simplified command taxonomy from #2884. Replaces the prior
 * Start / Pause / Resume / Stop (drain) / Force-stop cluster:
 *
 *   - **Start** — set `concurrency_cap` back to 1 (resume claiming).
 *   - **Stop** — graceful: block new spawns, let any in-flight agent
 *     finish its current phase. Previously spelled "Stop (drain)".
 *     No confirmation modal — stopping dev work is not destructive,
 *     and the operator needs an immediate stop button (not a friction
 *     path) when something goes sideways.
 *   - **Force Stop** — immediate: block new spawns AND abort any
 *     in-flight agent at the next phase boundary. Confirms via
 *     `ConfirmDialog` because it kills work in progress.
 *
 * Rendered as a flat inline cluster — no card wrapper, no explanatory
 * paragraph. The MFA re-auth gate was also removed (#2884) — admin
 * session auth is sufficient.
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
        onClick={() => onControlClick('stop')}
      >
        Stop
      </Button>
      <Button
        type="button"
        variant="destructive"
        size="sm"
        disabled={disabled || !daemonActive}
        onClick={() => onControlClick('force_stop')}
      >
        Force Stop
      </Button>
    </div>
  );
}
