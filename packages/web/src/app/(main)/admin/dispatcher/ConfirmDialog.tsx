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

interface ConfirmDialogProps {
  /** Non-null when the dialog should be open for this command. */
  command: DispatcherCommand | null;
  onConfirm: () => void;
  onCancel: () => void;
}

const DESCRIPTIONS: Partial<Record<DispatcherCommand, { title: string; body: string }>> = {
  stop: {
    title: 'Force-stop daemon?',
    body: 'This blocks new spawns and does not wait for in-flight agents to finish. In-flight agents may be killed abruptly, leaking worktrees. Prefer Stop (drain) unless the daemon is unresponsive.',
  },
  drain: {
    title: 'Stop (drain) daemon?',
    body: 'This blocks new spawns but lets in-flight agents finish their current /task iteration. Safer than Force-stop but may take several minutes.',
  },
  force_kill: {
    title: 'Force-kill agent?',
    body: 'Terminates the agent subprocess without waiting for a clean exit. Leaves the worktree in place for manual inspection. This cannot be undone.',
  },
};

export function ConfirmDialog({ command, onConfirm, onCancel }: ConfirmDialogProps) {
  const open = command !== null;
  const content = command ? DESCRIPTIONS[command] : null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{content?.title ?? 'Confirm'}</DialogTitle>
          <DialogDescription>{content?.body ?? ''}</DialogDescription>
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
