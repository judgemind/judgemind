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

const REASON_LABELS: Record<string, string> = {
  validation_fail: 'validation fail',
  all_null_guard: 'all-NULL metadata',
  governor_bio_route: 'governor bio',
  santa_clara_placeholder: 'SC placeholder',
  zero_rulings_no_text: 'no rulings, no text',
  no_extractable_text: 'unreadable PDF',
  no_ruling_text: 'no ruling text',
};

const INCOMPLETE_REASON_LABELS: Record<string, string> = {
  single_document_fallback: 'whole-document fallback',
  partial_split: 'partial split',
  exhausted_child: 'enrichment exhausted',
  dead_letter: 'dead-lettered',
  reingest_failed: 'reingest failed',
};

const fmt = (n: number) => n.toLocaleString('en-US');

export function formatIncompleteReasons(reasons: { reason: string; count: number }[]): string {
  return reasons
    .map((r) => `${INCOMPLETE_REASON_LABELS[r.reason] ?? r.reason} ${fmt(r.count)}`)
    .join(', ');
}

export function formatActionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

export function formatSkipReasons(scope: RawKeyReconcileScopeItem): string {
  if (scope.skippedByReason.length === 0) return '—';
  return scope.skippedByReason
    .map((r) => `${REASON_LABELS[r.reason] ?? r.reason} ${fmt(r.count)}`)
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

export function RawKeyReconciliationTable({ run }: { run: RawKeyReconciliationRun | null }) {
  if (!run) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        No reconciliation run recorded yet. The daily run writes the first one.
      </p>
    );
  }
  const issue = ISSUE_ACTIONS.has(run.action);
  return (
    <div>
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
          Last run {formatTimestamp(run.finishedAt)} ·{' '}
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
