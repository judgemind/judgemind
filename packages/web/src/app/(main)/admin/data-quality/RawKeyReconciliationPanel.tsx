'use client';

import { useQuery } from '@apollo/client';
import { InlineError } from '@/components/ErrorBanner';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  RAW_KEY_RECONCILIATION_QUERY,
  type RawKeyReconciliationData,
  type RawKeyReconciliationRun,
  type RawKeyReconcileScopeItem,
} from '@/lib/data-quality-queries';
import { SECTION_HEADING } from '@/lib/typography';

/**
 * Archived raw keys vs derived.documents, per archive scope (issue #5015).
 *
 * scripts/reconcile_raw_keys.py runs daily and sorts every archived raw key
 * into represented (has a document row), skipped (a recorded reason it has
 * none), pending (captured in the last few hours) or unrepresented (lost on
 * the way to the database). Two multi-week ingestion losses went unnoticed
 * before this existed.
 *
 * Incomplete keys (#5008) have rows that are not a complete split (a live
 * fallback or partial split, an exhausted child, a dead-lettered event, a
 * failed reingest): they are re-ingested daily up to a retry cap, then
 * reported as stuck.
 */

const ACTION_LABELS: Record<string, string> = {
  none: 'Nothing to heal',
  reingest: 'Auto-reingest launched',
  reingest_deferred: 'Reingest deferred (another reingest running)',
  report_only: 'Report only',
  over_threshold: 'Too many to auto-heal; issue filed',
  persistent: 'Reingest launched, but keys keep coming back; issue filed',
  reingest_failed: 'Reingest could not be launched; issue filed',
};

const ISSUE_ACTIONS = new Set(['over_threshold', 'persistent', 'reingest_failed']);

/**
 * Friendly names for every skip reason (telemetry.ingest_skips, ingest_skips.py
 * and skip_versions.py) and incomplete reason (telemetry.ingest_incomplete,
 * ingest_incomplete.py). One map serves both lists so a reason never shows as
 * a raw identifier in either (#5095).
 */
export const REASON_LABELS: Record<string, string> = {
  // Skip reasons.
  validation_fail: 'validation fail',
  all_null_guard: 'all-NULL metadata',
  governor_bio_route: 'governor bio',
  santa_clara_placeholder: 'SC placeholder',
  zero_rulings_no_text: 'no rulings, no text',
  no_extractable_text: 'unreadable PDF',
  no_ruling_text: 'no ruling text',
  // Incomplete key closed out because its raw object is gone (#5093).
  raw_missing: 'raw object missing',
  // Incomplete reasons.
  single_document_fallback: 'whole-document fallback',
  partial_split: 'partial split',
  exhausted_child: 'enrichment exhausted',
  dead_letter: 'dead-lettered',
  reingest_failed: 'reingest failed',
  all_children_rejected: 'all children rejected',
  evidence_unavailable: 'page unreadable',
  empty_extraction: 'nothing extracted',
  stale_validation: 'stale validation',
  // A skip verdict judged by an older rule or extractor, re-judged (#5094).
  stale_skip: 'stale skip verdict',
  // Every row superseded and the dedup ruling gone (#5093).
  orphaned_superseded: 'superseded, ruling lost',
};

export function formatReason(reason: string): string {
  return REASON_LABELS[reason] ?? reason;
}

const fmt = (n: number) => n.toLocaleString('en-US');

export function formatIncompleteReasons(reasons: { reason: string; count: number }[]): string {
  return reasons.map((r) => `${formatReason(r.reason)} ${fmt(r.count)}`).join(', ');
}

/** Fallback when an older API omits staleThresholdHours (#5095). */
const DEFAULT_STALE_THRESHOLD_HOURS = 36;

/**
 * The last completed run is stale: the API says so, or it finished more than
 * the threshold ago by the viewer's clock (a page left open keeps its cached
 * result), or its finish time is unknown (#5095).
 */
export function isReconcileStale(run: RawKeyReconciliationRun, now: Date = new Date()): boolean {
  if (run.isStale) return true;
  if (!run.finishedAt) return true;
  const ms = now.getTime() - new Date(run.finishedAt).getTime();
  if (Number.isNaN(ms)) return true;
  return ms / 3_600_000 > (run.staleThresholdHours ?? DEFAULT_STALE_THRESHOLD_HOURS);
}

/** Compact relative age, e.g. "40m ago", "37h ago", "5d ago". */
export function formatRunAge(iso: string | null, now: Date = new Date()): string | null {
  if (!iso) return null;
  const ms = now.getTime() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  const hours = Math.max(0, ms / 3_600_000);
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m ago`;
  if (hours < 72) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function formatActionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

export function formatSkipReasons(scope: RawKeyReconcileScopeItem): string {
  if (scope.skippedByReason.length === 0) return '—';
  return scope.skippedByReason
    .map((r) => `${formatReason(r.reason)} ${fmt(r.count)}`)
    .join(', ');
}

export function formatDayRange(scope: RawKeyReconcileScopeItem): string | null {
  const { oldestUnrepresentedDay: a, newestUnrepresentedDay: b } = scope;
  if (!a || !b) return null;
  return a === b ? a : `${a} – ${b}`;
}

function formatTimestamp(iso: string | null): string {
  return iso ? new Date(iso).toISOString().replace('T', ' ').slice(0, 16) + 'Z' : 'unknown';
}

export function RawKeyReconciliationTable({
  run,
  now,
}: {
  run: RawKeyReconciliationRun | null;
  now?: Date;
}) {
  if (!run) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        No reconciliation run recorded yet. The daily run writes the first one.
      </p>
    );
  }
  const issue = ISSUE_ACTIONS.has(run.action);
  const stale = isReconcileStale(run, now);
  const age = formatRunAge(run.finishedAt, now);
  const thresholdHours = run.staleThresholdHours ?? DEFAULT_STALE_THRESHOLD_HOURS;
  return (
    <div>
      {stale && (
        <p
          role="status"
          className="mb-3 rounded-md border border-red-300 px-3 py-2 text-sm text-red-800 dark:border-red-700 dark:text-red-300"
          data-testid="raw-reconcile-stale"
        >
          <span className="mr-2 inline-flex items-center rounded-full border border-red-300 px-2 py-0.5 text-xs font-medium dark:border-red-700">
            Stale
          </span>
          {age
            ? `The last successful run finished ${age}, more than ${thresholdHours}h ago`
            : 'The last successful run has no finish time'}
          : the daily reconciler has stopped running or is failing, so these counts are out of
          date. Check the open raw-reconcile-failure issue or the Raw-Key Reconciler workflow runs.
        </p>
      )}
      <p className="mb-3 text-sm text-muted-foreground" data-testid="raw-reconcile-summary">
        {run.unrepresented > 0 ? (
          <span className="font-medium text-red-700 dark:text-red-400">
            {fmt(run.unrepresented)} unrepresented
          </span>
        ) : (
          <span>None unrepresented</span>
        )}
        <span>
          {' '}
          · {fmt(run.represented)} represented · {fmt(run.skipped)} skipped
          {run.pending > 0 && ` · ${fmt(run.pending)} pending`} of {fmt(run.rawKeys)} raw keys
        </span>
        {(run.incomplete > 0 || run.incompleteStuck > 0) && (
          <span className="block" data-testid="raw-reconcile-incomplete">
            <span
              className={
                run.incompleteStuck > 0 ? 'font-medium text-red-700 dark:text-red-400' : undefined
              }
            >
              {fmt(run.incomplete)} incomplete retrying
              {run.incompleteStuck > 0 && `, ${fmt(run.incompleteStuck)} stuck at the retry cap`}
            </span>
            {run.incompleteByReason.length > 0 && (
              <span className="text-xs"> ({formatIncompleteReasons(run.incompleteByReason)})</span>
            )}
          </span>
        )}
        <span className="block text-xs">
          <span
            className={stale ? 'font-medium text-red-700 dark:text-red-400' : undefined}
            data-testid="raw-reconcile-last-run"
          >
            Last successful run {formatTimestamp(run.finishedAt)}
            {age && ` (${age})`}
          </span>{' '}
          ·{' '}
          <span className={issue ? 'text-red-700 dark:text-red-400' : undefined}>
            {formatActionLabel(run.action)}
          </span>
          {run.datedExcluded > 0 && ` · ${fmt(run.datedExcluded)} legacy dated keys not reconciled`}
        </span>
      </p>
      <div className="overflow-x-auto rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Scope</TableHead>
              <TableHead className="text-right">Raw keys</TableHead>
              <TableHead className="text-right">Represented</TableHead>
              <TableHead className="text-right">Skipped</TableHead>
              <TableHead className="text-right">Unrepresented</TableHead>
              <TableHead className="text-right">Incomplete</TableHead>
              <TableHead>Skip reasons</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {run.scopes.map((scope) => {
              const range = formatDayRange(scope);
              return (
                <TableRow
                  key={scope.scope}
                  className="hover:bg-transparent"
                  data-testid={`raw-reconcile-row-${scope.scope}`}
                >
                  <TableCell className="font-medium text-foreground">{scope.scope}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(scope.rawKeys)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(scope.represented)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(scope.skipped)}</TableCell>
                  <TableCell
                    className={`text-right tabular-nums ${
                      scope.unrepresented > 0 ? 'font-medium text-red-700 dark:text-red-400' : ''
                    }`}
                  >
                    {fmt(scope.unrepresented)}
                    {range && (
                      <span className="block text-xs font-normal text-muted-foreground">
                        {range}
                      </span>
                    )}
                    {scope.pending > 0 && (
                      <span className="block text-xs font-normal text-muted-foreground">
                        +{fmt(scope.pending)} pending
                      </span>
                    )}
                  </TableCell>
                  <TableCell
                    className={`text-right tabular-nums ${
                      scope.incompleteStuck > 0 ? 'font-medium text-red-700 dark:text-red-400' : ''
                    }`}
                  >
                    {fmt(scope.incomplete)}
                    {scope.incompleteStuck > 0 && (
                      <span className="block text-xs font-normal">
                        {fmt(scope.incompleteStuck)} stuck
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="max-w-xs text-xs text-muted-foreground">
                    {formatSkipReasons(scope)}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

export function RawKeyReconciliationPanel() {
  const { data, loading, error, refetch } = useQuery<RawKeyReconciliationData>(
    RAW_KEY_RECONCILIATION_QUERY,
  );

  return (
    <section aria-labelledby="raw-reconcile-heading">
      <h2 id="raw-reconcile-heading" className={SECTION_HEADING}>
        Raw-Key Reconciliation
      </h2>
      <p className="mb-4 mt-1 text-sm text-muted-foreground">
        Every archived raw capture should have a document row or a recorded skip reason. Unrepresented
        keys never reached the database; incomplete keys hold rows that are not a complete split.
        Small counts of both are re-ingested automatically each day.
      </p>
      {loading ? (
        <div className="h-48 animate-pulse rounded-lg bg-muted motion-reduce:animate-none" />
      ) : error ? (
        <InlineError message="Failed to load raw-key reconciliation." onRetry={() => void refetch()} />
      ) : (
        <RawKeyReconciliationTable run={data?.rawKeyReconciliation ?? null} />
      )}
    </section>
  );
}
