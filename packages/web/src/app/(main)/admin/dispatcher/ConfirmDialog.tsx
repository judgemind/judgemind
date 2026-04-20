'use client';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import type { DispatcherCommand } from '@/lib/dispatcher-queries';

/**
 * Commands that can open the ConfirmDialog. After #2884's command-
 * surface simplification, only `force_stop` needs the modal — `stop`
 * is graceful and doesn't kill in-flight work, so it fires without
 * confirmation.
 *
 * Also includes the synthetic `'lower_cap'` sentinel used by the
 * config strip when the operator reduces `concurrency_cap`
 * (#2805 §1.6; spec §17 Risk 6 Option A).
 */
export type ConfirmableCommand = DispatcherCommand | 'lower_cap';

interface ConfirmDialogProps {
  /** Non-null when the dialog should be open for this command. */
  command: ConfirmableCommand | null;
  /**
   * When `command === 'lower_cap'`, carries the before/after values so
   * the dialog's copy can explain the transition. Ignored otherwise.
   */
  capDetail?: { current: number; next: number } | null;
  onConfirm: () => void;
  onCancel: () => void;
}

const DESCRIPTIONS: Partial<Record<DispatcherCommand, { title: string; body: string }>> = {
  force_stop: {
    title: 'Force-stop daemon?',
    body: 'This blocks new spawns AND aborts any in-flight agent at the next phase boundary. In-flight work is lost. Prefer Stop (graceful) unless the daemon is unresponsive or you need to halt immediately.',
  },
};

export function ConfirmDialog({
  command,
  capDetail,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const open = command !== null;
  let title = 'Confirm';
  let body = '';
  if (command === 'lower_cap' && capDetail) {
    title = `Lower concurrency cap from ${capDetail.current} to ${capDetail.next}?`;
    body =
      'In-flight agents continue; the daemon stops claiming new ones until the cap is raised or slots free up.';
  } else if (command && command !== 'lower_cap') {
    const content = DESCRIPTIONS[command];
    if (content) {
      title = content.title;
      body = content.body;
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            onClick={onConfirm}
            data-testid="confirm-dialog-confirm"
          >
            Confirm
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
