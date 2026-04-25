'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { DispatcherConfigEntry } from '@/lib/dispatcher-queries';

interface ConfigPanelProps {
  entries: readonly DispatcherConfigEntry[];
  spawnFrozenUntil: string | null;
  /**
   * Called when the operator commits an edit. The parent wires this to
   * `dispatcherSetConfig` with the MFA placeholder header + a
   * `ConfirmDialog` for destructive transitions (e.g. lowering cap).
   *
   * - `key`    — config key, e.g. `"concurrency_cap"`.
   * - `value`  — JSON-encoded string ready for the mutation.
   *
   * The parent resolves the returned promise on success and rejects on
   * failure; the ConfigPanel uses that to show a 1 s green success tint
   * or surface the error via `errorMessage`.
   */
  onCommitEdit: (key: string, value: string) => Promise<void>;
  /** Transient error surfaced by the parent (e.g. validation failure). */
  errorMessage?: string | null;
  /** Transient busy indicator for the last-edited key. */
  busyKey?: string | null;
}

/**
 * Compact editable config strip (#2805 §1.6). Replaces the two-column
 * `<dl>` with a single horizontal row:
 *
 *   cap [ 1 ]   backoff [ 300s ]
 *
 * When the spawn-frozen back-pressure gate is active:
 *
 *   cap [ 1 ]   backoff [ 300s ]   new agents paused for ~N min
 *   cap [ 1 ]   backoff [ 300s ]   new agents paused until H:MM AM/PM PT
 *
 * The freeze label is structurally hidden when inactive (not visibility:hidden
 * or opacity:0 — it is simply not rendered). See #2990.
 *
 * Borderless. Sits beneath the main two-column grid.
 *
 * Editing semantics:
 *   - Click a value → switch to input.
 *   - Enter / blur → parent's `onCommitEdit` (parent handles mutation +
 *     ConfirmDialog for destructive transitions).
 *   - Escape → cancel.
 *
 * Non-editable display-only: `spawn_frozen_until` (when active).
 */
export function ConfigPanel({
  entries,
  spawnFrozenUntil,
  onCommitEdit,
  errorMessage,
  busyKey,
}: ConfigPanelProps) {
  const entryByKey = new Map(entries.map((e) => [e.key, e] as const));
  const freezeLabel = buildFreezeLabel(spawnFrozenUntil);

  return (
    <section
      aria-labelledby="config-heading"
      className="border-t border-border pt-3 mt-4"
      data-testid="config-strip"
    >
      <h2 id="config-heading" className="sr-only">
        Dispatcher config
      </h2>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 font-mono text-sm">
        <EditableNumberField
          label="cap"
          entry={entryByKey.get('concurrency_cap') ?? null}
          min={0}
          max={5}
          busy={busyKey === 'concurrency_cap'}
          onCommit={(value) => onCommitEdit('concurrency_cap', String(value))}
        />
        <span aria-hidden="true" className="text-muted-foreground">&middot;</span>
        <EditableBackoffField
          entry={entryByKey.get('backoff_seconds') ?? null}
          busy={busyKey === 'backoff_seconds'}
          onCommit={(value) => onCommitEdit('backoff_seconds', value)}
        />
        {freezeLabel !== null && (
          <>
            <span aria-hidden="true" className="text-muted-foreground">&middot;</span>
            <span
              className="text-foreground"
              title={freezeLabel.tooltip}
            >
              {freezeLabel.text}
            </span>
          </>
        )}
      </div>
      {errorMessage && (
        <p
          role="alert"
          className="mt-2 text-xs text-red-700 dark:text-red-300"
          data-testid="config-error"
        >
          {errorMessage}
        </p>
      )}
    </section>
  );
}

/**
 * Formats a Pacific Time clock string from a Date, e.g. "9:42 PM PT".
 */
function formatPacificTime(date: Date): string {
  return (
    new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/Los_Angeles',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }).format(date) + ' PT'
  );
}

/**
 * Computes the display text and tooltip for the spawn-frozen label.
 * Returns null when the freeze is inactive (null or in the past).
 *
 * - ≤15 min remaining → `new agents paused for ~N min`
 * - >15 min remaining → `new agents paused until H:MM AM/PM PT`
 *
 * Tooltip: `Resumes {H:MM AM/PM PT}.`
 */
function buildFreezeLabel(
  spawnFrozenUntil: string | null,
): { text: string; tooltip: string } | null {
  if (spawnFrozenUntil === null) return null;

  const until = new Date(spawnFrozenUntil);
  const now = Date.now();
  const remainingMs = until.getTime() - now;

  // Past or exactly now → hide.
  if (remainingMs <= 0) return null;

  const resumeTime = formatPacificTime(until);
  const tooltip = `Resumes ${resumeTime}.`;

  const remainingMinutes = Math.max(1, Math.round(remainingMs / 60_000));

  if (remainingMinutes <= 15) {
    return {
      text: `new agents paused for ~${remainingMinutes} min`,
      tooltip,
    };
  }

  return {
    text: `new agents paused until ${resumeTime}`,
    tooltip,
  };
}

interface EditableNumberFieldProps {
  label: string;
  entry: DispatcherConfigEntry | null;
  min: number;
  max: number;
  busy?: boolean;
  onCommit: (value: number) => Promise<void> | void;
}

function EditableNumberField({
  label,
  entry,
  min,
  max,
  busy,
  onCommit,
}: EditableNumberFieldProps) {
  const parsed = entry ? safeParseNumber(entry.value) : null;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string>(parsed !== null ? String(parsed) : '');
  const [flashSuccess, setFlashSuccess] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  useEffect(() => {
    if (!editing && parsed !== null) {
      setDraft(String(parsed));
    }
  }, [editing, parsed]);

  const startEdit = useCallback(() => {
    setEditing(true);
    setDraft(parsed !== null ? String(parsed) : '');
  }, [parsed]);

  const commit = useCallback(async () => {
    const n = Number.parseInt(draft, 10);
    if (!Number.isFinite(n) || n < min || n > max) {
      setEditing(false);
      if (parsed !== null) setDraft(String(parsed));
      return;
    }
    if (parsed !== null && n === parsed) {
      setEditing(false);
      return;
    }
    setEditing(false);
    try {
      await onCommit(n);
      setFlashSuccess(true);
      window.setTimeout(() => setFlashSuccess(false), 1000);
    } catch {
      // Parent surfaces the error via `errorMessage`. Restore the prior
      // value so the UI is not out-of-sync with the server.
      if (parsed !== null) setDraft(String(parsed));
    }
  }, [draft, min, max, onCommit, parsed]);

  const cancel = useCallback(() => {
    setEditing(false);
    if (parsed !== null) setDraft(String(parsed));
  }, [parsed]);

  if (editing) {
    return (
      <label className="inline-flex items-center gap-1">
        <span className="text-muted-foreground">{label}</span>
        <input
          ref={inputRef}
          type="number"
          min={min}
          max={max}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void commit()}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void commit();
            } else if (e.key === 'Escape') {
              e.preventDefault();
              cancel();
            }
          }}
          className="h-7 w-14 rounded border border-input bg-background px-1 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-testid={`config-input-${entry?.key ?? label}`}
        />
      </label>
    );
  }

  return (
    <button
      type="button"
      onClick={startEdit}
      disabled={busy}
      className={`inline-flex items-center gap-1 rounded px-1 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60 ${
        flashSuccess ? 'bg-green-100 dark:bg-green-900/30' : ''
      }`}
      data-testid={`config-value-${entry?.key ?? label}`}
      aria-label={`Edit ${label}`}
    >
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground">
        [{parsed !== null ? parsed : '—'}]
      </span>
    </button>
  );
}

interface EditableBackoffFieldProps {
  entry: DispatcherConfigEntry | null;
  busy?: boolean;
  onCommit: (value: string) => Promise<void> | void;
}

/**
 * Specialized editor for `backoff_seconds` — the DB shape is an integer
 * array, but the operator thinks in terms of the first retry delay. We
 * show the first entry as "[Ns]" and allow editing it; the commit
 * preserves the rest of the array (or defaults to [N]).
 */
function EditableBackoffField({ entry, busy, onCommit }: EditableBackoffFieldProps) {
  const parsedArray = entry ? safeParseArray(entry.value) : null;
  const firstValue = parsedArray && parsedArray.length > 0 ? parsedArray[0] : null;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string>(firstValue !== null ? String(firstValue) : '');
  const [flashSuccess, setFlashSuccess] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  useEffect(() => {
    if (!editing && firstValue !== null) {
      setDraft(String(firstValue));
    }
  }, [editing, firstValue]);

  const commit = useCallback(async () => {
    const n = Number.parseInt(draft, 10);
    if (!Number.isFinite(n) || n < 0) {
      setEditing(false);
      if (firstValue !== null) setDraft(String(firstValue));
      return;
    }
    const nextArray = parsedArray && parsedArray.length > 0
      ? [n, ...parsedArray.slice(1)]
      : [n];
    if (firstValue !== null && n === firstValue) {
      setEditing(false);
      return;
    }
    setEditing(false);
    try {
      await onCommit(JSON.stringify(nextArray));
      setFlashSuccess(true);
      window.setTimeout(() => setFlashSuccess(false), 1000);
    } catch {
      if (firstValue !== null) setDraft(String(firstValue));
    }
  }, [draft, firstValue, parsedArray, onCommit]);

  const cancel = useCallback(() => {
    setEditing(false);
    if (firstValue !== null) setDraft(String(firstValue));
  }, [firstValue]);

  if (editing) {
    return (
      <label className="inline-flex items-center gap-1">
        <span className="text-muted-foreground">backoff</span>
        <input
          ref={inputRef}
          type="number"
          min={0}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void commit()}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void commit();
            } else if (e.key === 'Escape') {
              e.preventDefault();
              cancel();
            }
          }}
          className="h-7 w-16 rounded border border-input bg-background px-1 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-testid="config-input-backoff_seconds"
        />
        <span className="text-muted-foreground">s</span>
      </label>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      disabled={busy}
      className={`inline-flex items-center gap-1 rounded px-1 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60 ${
        flashSuccess ? 'bg-green-100 dark:bg-green-900/30' : ''
      }`}
      data-testid="config-value-backoff_seconds"
      aria-label="Edit backoff"
    >
      <span className="text-muted-foreground">backoff</span>
      <span className="text-foreground">
        [{firstValue !== null ? `${firstValue}s` : '—'}]
      </span>
    </button>
  );
}

function safeParseNumber(jsonValue: string): number | null {
  try {
    const raw = JSON.parse(jsonValue);
    if (typeof raw === 'number' && Number.isFinite(raw)) return raw;
    return null;
  } catch {
    return null;
  }
}

function safeParseArray(jsonValue: string): number[] | null {
  try {
    const raw = JSON.parse(jsonValue);
    if (Array.isArray(raw) && raw.every((x) => typeof x === 'number' && Number.isFinite(x))) {
      return raw as number[];
    }
    return null;
  } catch {
    return null;
  }
}
