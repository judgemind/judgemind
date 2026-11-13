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
  ROSTER_FRESHNESS_QUERY,
  type RosterFreshnessData,
  type RosterFreshnessItem,
} from '@/lib/data-quality-queries';
import { SECTION_HEADING } from '@/lib/typography';

/**
 * Per-court judge-roster fetch health (issue #4872).
 *
 * A failed roster prefetch used to be visible only in scraper logs, so the SF
 * roster stayed stale for ~7 months and Santa Clara failed on every run
 * unnoticed. This panel lists every roster court with its last successful
 * fetch, last attempt and error class, and flags courts with no success
 * inside the staleness threshold.
 */

const STATUS_STYLES: Record<RosterFreshnessItem['status'], { label: string; className: string }> = {
  ok: {
    label: 'OK',
    className:
      'border-green-300 text-green-800 dark:border-green-700 dark:text-green-300',
  },
  failing: {
    label: 'Failing',
    className:
      'border-yellow-300 text-yellow-800 dark:border-yellow-700 dark:text-yellow-300',
  },
  stale: {
    label: 'Stale',
    className: 'border-red-300 text-red-800 dark:border-red-700 dark:text-red-300',
  },
};

const ERROR_CLASS_LABELS: Record<string, string> = {
  http_403: 'HTTP 403 (blocked)',
  http_4xx: 'HTTP 4xx',
  http_5xx: 'HTTP 5xx',
  redirect: 'Redirect',
  network: 'Network / timeout',
  empty_roster: '0 departments parsed',
  missing_court_id: 'Missing COURT_ID',
  import_error: 'Import error',
  other: 'Other error',
};

/** Human-readable court name from an id like `ca_san_francisco`. */
export function formatCourtId(courtId: string): string {
  const parts = courtId.split('_');
  const [state, ...rest] = parts;
  if (rest.length === 0) return courtId;
  const county = rest.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  return `${county} (${state.toUpperCase()})`;
}

/** Compact relative age, e.g. "3h ago", "12d ago". */
export function formatAge(iso: string | null, now: Date = new Date()): string {
  if (!iso) return 'Never';
  const ms = now.getTime() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return 'Unknown';
  const hours = Math.max(0, ms / 3_600_000);
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m ago`;
  if (hours < 48) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function formatErrorClass(item: RosterFreshnessItem): string | null {
  if (item.lastOutcome !== 'failure' || !item.lastErrorClass) return null;
  const label = ERROR_CLASS_LABELS[item.lastErrorClass] ?? item.lastErrorClass;
  if (item.lastHttpStatus && !label.includes(String(item.lastHttpStatus))) {
    return `${label} · ${item.lastHttpStatus}`;
  }
  return label;
}

function StatusBadge({ status }: { status: RosterFreshnessItem['status'] }) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.stale;
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${style.className}`}
    >
      {style.label}
    </span>
  );
}

function formatTimestamp(iso: string | null): string | undefined {
  return iso ? new Date(iso).toISOString().replace('T', ' ').slice(0, 16) + 'Z' : undefined;
}

export function RosterHealthTable({ items }: { items: RosterFreshnessItem[] }) {
  const staleCount = items.filter((i) => i.isStale).length;
  const failingCount = items.filter((i) => i.status === 'failing').length;
  const thresholdDays = items.length > 0 ? Math.round(items[0].staleThresholdHours / 24) : 7;

  if (items.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        No roster fetches recorded yet. Rows appear after the next scraper run.
      </p>
    );
  }

  return (
    <div>
      <p className="mb-3 text-sm text-muted-foreground">
        {staleCount > 0 ? (
          <span className="font-medium text-red-700 dark:text-red-400">
            {staleCount} stale
          </span>
        ) : (
          <span>None stale</span>
        )}
        {failingCount > 0 && <span> · {failingCount} failing</span>}
        <span> · stale after {thresholdDays} days without a successful fetch</span>
      </p>
      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Court</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Last success</TableHead>
              <TableHead>Last attempt</TableHead>
              <TableHead>Error</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => {
              const errorLabel = formatErrorClass(item);
              return (
                <TableRow
                  key={item.courtId}
                  className="hover:bg-transparent"
                  data-testid={`roster-row-${item.courtId}`}
                  data-status={item.status}
                >
                  <TableCell className="font-medium text-foreground">
                    {formatCourtId(item.courtId)}
                    {item.lastDepartmentCount !== null && item.lastOutcome === 'success' && (
                      <span className="block text-xs font-normal text-muted-foreground">
                        {item.lastDepartmentCount} departments
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={item.status} />
                  </TableCell>
                  <TableCell
                    className={item.isStale ? 'text-red-700 dark:text-red-400' : 'text-muted-foreground'}
                    title={formatTimestamp(item.lastSuccessAt)}
                  >
                    {formatAge(item.lastSuccessAt)}
                  </TableCell>
                  <TableCell className="text-muted-foreground" title={formatTimestamp(item.lastAttemptAt)}>
                    {item.lastAttemptAt ? (
                      <>
                        {formatAge(item.lastAttemptAt)}
                        {item.lastOutcome && (
                          <span className="block text-xs">
                            {item.lastOutcome === 'success' ? 'succeeded' : 'failed'}
                            {item.consecutiveFailures > 1 &&
                              ` · ${item.consecutiveFailures} failures in a row`}
                          </span>
                        )}
                      </>
                    ) : (
                      'Not recorded'
                    )}
                  </TableCell>
                  <TableCell className="max-w-xs text-muted-foreground">
                    {errorLabel ? (
                      <span title={item.lastErrorMessage ?? undefined}>
                        <span className="text-red-700 dark:text-red-400">{errorLabel}</span>
                        {item.lastErrorMessage && (
                          <span className="block truncate text-xs">{item.lastErrorMessage}</span>
                        )}
                      </span>
                    ) : (
                      '—'
                    )}
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

export function RosterHealthPanel() {
  const { data, loading, error, refetch } = useQuery<RosterFreshnessData>(ROSTER_FRESHNESS_QUERY);

  return (
    <section aria-labelledby="roster-health-heading">
      <h2 id="roster-health-heading" className={SECTION_HEADING}>
        Judge Roster Freshness
      </h2>
      <p className="mb-4 mt-1 text-sm text-muted-foreground">
        Department-to-judge directories fetched before each scraper run. A stale roster degrades judge
        attribution for that county.
      </p>
      {loading ? (
        <div className="h-48 animate-pulse rounded-lg bg-muted motion-reduce:animate-none" />
      ) : error ? (
        <InlineError message="Failed to load roster freshness." onRetry={() => void refetch()} />
      ) : (
        <RosterHealthTable items={data?.rosterFreshness ?? []} />
      )}
    </section>
  );
}
