/**
 * Unit tests for the DB-only enrichment path on the dispatcher admin
 * resolvers (issue #2820). These replace the deleted
 * ``dispatcher-github.unit.test.ts`` which covered the old fetch-
 * from-GitHub fallback. The new tests exercise:
 *
 *   - ``normalizeSnapshotJson`` — accepts either parsed jsonb or a
 *     JSON-encoded string, drops malformed rows, projects labels to
 *     string[].
 *   - ``queueItemFromSnapshot`` — builds the QueueItem shape the
 *     admin page expects, including ``priority`` extracted from
 *     labels and (when requested) ``blockedBy`` parsed from the body.
 *   - ``recentCompletionsToGraphQL`` — reads ``issue_title`` directly
 *     from the ``dispatcher.agents`` row; falls back to null for
 *     rows that pre-date migration 28.
 *   - The ``parse-labels`` helpers remain pure — no fetch.
 */

import { describe, it, expect } from 'vitest';
import {
  CATEGORY_DISPLAY_NAMES,
  coerceNullableNumber,
  displayCategoryFor,
  failureRowToGraphQL,
  normalizeSnapshotJson,
  phaseCostRowsToGraphQL,
  queueItemFromSnapshot,
  recentCompletionsToGraphQL,
  sortAndSliceQueueBlocked,
  sortAndSliceQueueReady,
  sumPhaseCost,
  type SnapshotIssueRecord,
} from '../src/graphql/dispatcher/resolvers';
import {
  extractPriority,
  parseBlockedBy,
  priorityRank,
  PRIORITY_RANK_NO_LABEL,
} from '../src/graphql/dispatcher/parse-labels';

describe('normalizeSnapshotJson', () => {
  it('accepts already-parsed jsonb arrays', () => {
    const raw = [
      {
        number: 42,
        title: 'Do the thing',
        labels: ['priority/p1', 'agent/ready'],
        createdAt: '2026-04-18T00:00:00Z',
      },
    ];
    const result = normalizeSnapshotJson(raw);
    expect(result).toEqual([
      {
        number: 42,
        title: 'Do the thing',
        labels: ['priority/p1', 'agent/ready'],
        createdAt: '2026-04-18T00:00:00Z',
      },
    ]);
  });

  it('accepts a JSON-encoded string (defensive)', () => {
    const raw = JSON.stringify([
      {
        number: 7,
        title: 't',
        labels: ['area/x'],
        createdAt: '2026-04-18T00:00:00Z',
      },
    ]);
    const result = normalizeSnapshotJson(raw);
    expect(result).toHaveLength(1);
    expect(result[0].number).toBe(7);
  });

  it('returns empty array for invalid string JSON', () => {
    expect(normalizeSnapshotJson('not json')).toEqual([]);
  });

  it('returns empty array for non-array input', () => {
    expect(normalizeSnapshotJson(null)).toEqual([]);
    expect(normalizeSnapshotJson({})).toEqual([]);
    expect(normalizeSnapshotJson(42)).toEqual([]);
  });

  it('drops entries without a numeric ``number``', () => {
    const raw = [
      { number: 1, title: 'ok', labels: [], createdAt: '' },
      { number: 'bad', title: 'skip', labels: [], createdAt: '' },
      { title: 'no-number', labels: [], createdAt: '' },
    ];
    const result = normalizeSnapshotJson(raw);
    expect(result).toHaveLength(1);
    expect(result[0].number).toBe(1);
  });

  it('filters non-string labels', () => {
    const raw = [
      {
        number: 1,
        title: 't',
        labels: ['ok', 42, { name: 'obj' }, null],
        createdAt: '',
      },
    ];
    const [entry] = normalizeSnapshotJson(raw);
    expect(entry.labels).toEqual(['ok']);
  });

  it('preserves ``body`` when present (blocked snapshot shape)', () => {
    const raw = [
      {
        number: 100,
        title: 't',
        labels: [],
        createdAt: '',
        body: 'Blocked by #42\n',
      },
    ];
    const [entry] = normalizeSnapshotJson(raw);
    expect(entry.body).toBe('Blocked by #42\n');
  });

  it('preserves explicit null ``body``', () => {
    const raw = [
      { number: 1, title: 't', labels: [], createdAt: '', body: null },
    ];
    const [entry] = normalizeSnapshotJson(raw);
    expect(entry.body).toBeNull();
  });
});

describe('queueItemFromSnapshot', () => {
  const base: SnapshotIssueRecord = {
    number: 999,
    title: 'Test issue',
    labels: ['priority/p2', 'agent/ready', 'area/api'],
    createdAt: '2026-04-18T12:00:00Z',
  };

  it('builds the QueueItem shape (includeBlockedBy=false)', () => {
    const result = queueItemFromSnapshot(base, false);
    expect(result).toEqual({
      issueNumber: 999,
      title: 'Test issue',
      priority: 'p2',
      labels: ['priority/p2', 'agent/ready', 'area/api'],
      createdAt: '2026-04-18T12:00:00Z',
      blockedBy: [],
      cooldownSecondsRemaining: null,
    });
  });

  it('parses blockedBy from the body when requested', () => {
    const withBody: SnapshotIssueRecord = {
      ...base,
      body: 'Some context.\n\nBlocked by #42\nBlocked by #100\n',
    };
    const result = queueItemFromSnapshot(withBody, true);
    expect(result.blockedBy).toEqual([42, 100]);
  });

  it('handles missing priority label', () => {
    const noPriority: SnapshotIssueRecord = { ...base, labels: ['area/api'] };
    const result = queueItemFromSnapshot(noPriority, false);
    expect(result.priority).toBeNull();
  });

  it('returns empty blockedBy when body is missing and includeBlockedBy=true', () => {
    const result = queueItemFromSnapshot(base, true);
    expect(result.blockedBy).toEqual([]);
  });
});

describe('recentCompletionsToGraphQL', () => {
  it('passes ``issue_title`` and ``priority`` through from the agents row', () => {
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: 'Fix the thing',
        // #2899 — priority is captured at claim time.
        priority: 'p2',
        status: 'succeeded',
        // #3024 — started_at paired with ended_at so the admin cockpit
        // tooltip can render Start/Duration/End.
        started_at: '2026-04-18T11:50:00Z',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: 2824,
        total_tokens: 12345,
        total_cost_usd: '0.0420',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result).toEqual([
      {
        agentId: 'uuid-1',
        issueNumber: 100,
        issueTitle: 'Fix the thing',
        priority: 'p2',
        status: 'succeeded',
        startedAt: '2026-04-18T11:50:00Z',
        endedAt: '2026-04-18T12:00:00Z',
        prNumber: 2824,
        totalTokens: 12345,
        totalCostUsd: 0.042,
        // #2900: succeeded rows stay NULL on failure_summary — the
        // column only populates on failure terminals.
        failureSummary: null,
        // #2953: milestone columns default to null when the input row
        // omits them (pre-migration-35 historical case).
        mergedAt: null,
        verifiedAt: null,
        verifySkipReason: null,
        retroedAt: null,
      },
    ]);
  });

  // #3024 — started_at passthrough.
  it('#3024: passes ``started_at`` through verbatim for the Start/End tooltip', () => {
    const rows = [
      {
        agent_id: 'uuid-3024',
        issue_number: 3024,
        issue_title: 'Tooltip test',
        status: 'succeeded',
        started_at: '2026-04-22T16:41:16Z',
        ended_at: '2026-04-22T16:56:33Z',
        pr_number: 3025,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].startedAt).toBe('2026-04-22T16:41:16Z');
    expect(result[0].endedAt).toBe('2026-04-22T16:56:33Z');
  });

  // #2900: failure_summary passthrough + trimming/empty handling.
  it('passes ``failure_summary`` through for failure rows', () => {
    const rows = [
      {
        agent_id: 'uuid-fail',
        issue_number: 2900,
        issue_title: 'Test failure summary',
        status: 'failed',
        ended_at: '2026-04-20T10:00:00Z',
        pr_number: null,
        failure_summary:
          'ralph crashed at ralph-reviewer iteration 3 (subprocess_turn_limit): reviewer exceeded max turns',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].failureSummary).toBe(
      'ralph crashed at ralph-reviewer iteration 3 (subprocess_turn_limit): reviewer exceeded max turns',
    );
  });

  it('emits ``failureSummary=null`` for NULL / empty values (pre-migration-33 rows)', () => {
    const rows = [
      // Historical row — column didn't exist at write-time.
      {
        agent_id: 'uuid-old',
        issue_number: 999,
        issue_title: 'Old row',
        status: 'failed',
        ended_at: '2026-03-01T00:00:00Z',
        pr_number: null,
        failure_summary: null,
      },
      // Edge: empty string sneaks through.
      {
        agent_id: 'uuid-empty',
        issue_number: 998,
        issue_title: 'Empty summary',
        status: 'failed',
        ended_at: '2026-03-01T00:00:00Z',
        pr_number: null,
        failure_summary: '',
      },
      // Whitespace-only also collapses to null.
      {
        agent_id: 'uuid-ws',
        issue_number: 997,
        issue_title: 'Whitespace summary',
        status: 'failed',
        ended_at: '2026-03-01T00:00:00Z',
        pr_number: null,
        failure_summary: '   ',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].failureSummary).toBeNull();
    expect(result[1].failureSummary).toBeNull();
    expect(result[2].failureSummary).toBeNull();
  });

  it('trims surrounding whitespace on ``failure_summary``', () => {
    const rows = [
      {
        agent_id: 'uuid-wsp',
        issue_number: 111,
        issue_title: 'Padded',
        status: 'crashed',
        ended_at: '2026-04-20T10:00:00Z',
        pr_number: null,
        failure_summary: '  ralph crashed (stuck_timeout): no log tail  ',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].failureSummary).toBe(
      'ralph crashed (stuck_timeout): no log tail',
    );
  });

  // #2913 — defense-in-depth: even if a pre-#2913 ``succeeded`` row is
  // still carrying a stale ``failure_summary`` in the DB (e.g. the 3
  // confused rows observed on dev: agents for #2921, #2916, #2899 that
  // went crashed → retry_reset → succeeded before the daemon-side
  // clear shipped), the resolver MUST NOT expose it on the GraphQL
  // surface. The admin cockpit renders the ✓ glyph with the default
  // status-label tooltip instead. Primary fix is the daemon-side clear;
  // this is the belt-and-suspenders so the UX never regresses.
  it('drops ``failureSummary`` on succeeded rows even if the DB still has one (#2913)', () => {
    const rows = [
      {
        agent_id: 'uuid-recovered',
        issue_number: 2908,
        issue_title: 'Recovered via retry_reset',
        status: 'succeeded',
        ended_at: '2026-04-20T12:00:00Z',
        pr_number: 2912,
        failure_summary:
          'daemon_restart_abandoned crashed at daemon_restart_abandoned (daemon_restart_abandoned)',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].status).toBe('succeeded');
    expect(result[0].failureSummary).toBeNull();
  });

  it('drops ``failureSummary`` on needs_review rows even if the DB still has one (#2913)', () => {
    // ``needs_review`` is a correct-outcome terminal — the draft PR IS
    // the signal, not a failure string. Same gate as ``succeeded``.
    const rows = [
      {
        agent_id: 'uuid-nr',
        issue_number: 2856,
        issue_title: 'Draft PR opened',
        status: 'needs_review',
        ended_at: '2026-04-20T14:00:00Z',
        pr_number: 9001,
        failure_summary: 'stale summary from an earlier iteration',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].status).toBe('needs_review');
    expect(result[0].failureSummary).toBeNull();
  });

  it('keeps ``failureSummary`` on failed / crashed / plan_blocked rows (#2913 gate allows failure statuses)', () => {
    // Positive case: the resolver-side gate must only drop the summary
    // for correct-outcome terminals. Failure terminals still surface
    // the tooltip.
    const rows = [
      {
        agent_id: 'uuid-failed',
        issue_number: 101,
        status: 'failed',
        ended_at: '2026-04-20T10:00:00Z',
        pr_number: null,
        failure_summary: 'ralph failed at ralph-reviewer iteration 3',
      },
      {
        agent_id: 'uuid-crashed',
        issue_number: 102,
        status: 'crashed',
        ended_at: '2026-04-20T10:01:00Z',
        pr_number: null,
        failure_summary: 'ralph crashed (stuck_timeout): no log tail',
      },
      {
        agent_id: 'uuid-pb',
        issue_number: 103,
        status: 'plan_blocked',
        ended_at: '2026-04-20T10:02:00Z',
        pr_number: null,
        failure_summary:
          'plan phase returned go=false (plan_go_false): scope is ambiguous',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].failureSummary).toBe(
      'ralph failed at ralph-reviewer iteration 3',
    );
    expect(result[1].failureSummary).toBe(
      'ralph crashed (stuck_timeout): no log tail',
    );
    expect(result[2].failureSummary).toBe(
      'plan phase returned go=false (plan_go_false): scope is ambiguous',
    );
  });

  it('emits priority=null for pre-migration-33 rows (#2899)', () => {
    // Rows whose claim predates migration 34 (the one #2899 adds) have
    // ``priority`` = NULL from pg; the UI renders an em-dash placeholder.
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: 'Legacy agent',
        priority: null,
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: null,
      },
      {
        agent_id: 'uuid-2',
        issue_number: 101,
        issue_title: 'Agent on unlabelled issue',
        // column missing entirely (defensive — pg result shape drift)
        status: 'succeeded',
        ended_at: '2026-04-18T13:00:00Z',
        pr_number: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].priority).toBeNull();
    expect(result[1].priority).toBeNull();
  });

  // #2953 — milestone-column passthrough: the resolver must surface
  // merged_at / verified_at / verify_skip_reason / retroed_at verbatim
  // so the admin cockpit can render a single glyph whose colour
  // encodes pipeline completeness.
  it('#2953: passes all four milestone columns through verbatim', () => {
    const rows = [
      {
        agent_id: 'uuid-fully-shipped',
        issue_number: 2953,
        issue_title: 'Fully-shipped row',
        status: 'succeeded',
        ended_at: '2026-04-20T22:50:00Z',
        pr_number: 3000,
        merged_at: '2026-04-20T22:35:00Z',
        verified_at: '2026-04-20T22:41:00Z',
        verify_skip_reason: null,
        retroed_at: '2026-04-20T22:50:00Z',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].mergedAt).toBe('2026-04-20T22:35:00Z');
    expect(result[0].verifiedAt).toBe('2026-04-20T22:41:00Z');
    expect(result[0].verifySkipReason).toBeNull();
    expect(result[0].retroedAt).toBe('2026-04-20T22:50:00Z');
  });

  it('#2953: surfaces verifySkipReason on a self-deploy row', () => {
    const rows = [
      {
        agent_id: 'uuid-self-deploy',
        issue_number: 2953,
        issue_title: 'Dispatcher self-PR',
        status: 'succeeded',
        ended_at: '2026-04-20T22:48:00Z',
        pr_number: 3001,
        merged_at: '2026-04-20T22:35:00Z',
        verified_at: null,
        verify_skip_reason: 'self_deploy',
        retroed_at: '2026-04-20T22:48:00Z',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].verifySkipReason).toBe('self_deploy');
    expect(result[0].verifiedAt).toBeNull();
  });

  it('#2953: emits all milestone columns null for pre-migration-35 rows', () => {
    // A historical row whose agent ran before migration 35 has NULL
    // in the four new columns (backfill only sets merged_at for
    // rows with pr_number + status='succeeded'; rows without a PR
    // stay untouched).
    const rows = [
      {
        agent_id: 'uuid-pre35',
        issue_number: 100,
        issue_title: 'Pre-migration row',
        status: 'succeeded',
        ended_at: '2026-01-01T00:00:00Z',
        pr_number: null,
        // columns missing entirely — defensive against pg result shape drift
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].mergedAt).toBeNull();
    expect(result[0].verifiedAt).toBeNull();
    expect(result[0].verifySkipReason).toBeNull();
    expect(result[0].retroedAt).toBeNull();
  });

  it('#2953: verifySkipReason ignores empty / non-string values', () => {
    // Defense-in-depth: empty strings or non-string jsonb drift
    // must collapse to null so the UI's milestone-completeness
    // logic doesn't treat an empty string as "skipped".
    const rows = [
      {
        agent_id: 'uuid-empty',
        issue_number: 100,
        issue_title: 'Empty skip reason',
        status: 'succeeded',
        ended_at: '2026-04-20T00:00:00Z',
        pr_number: 500,
        merged_at: '2026-04-20T00:00:00Z',
        verify_skip_reason: '',
      },
      {
        agent_id: 'uuid-nonstring',
        issue_number: 101,
        issue_title: 'Non-string skip reason',
        status: 'succeeded',
        ended_at: '2026-04-20T00:00:00Z',
        pr_number: 501,
        merged_at: '2026-04-20T00:00:00Z',
        verify_skip_reason: 42,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].verifySkipReason).toBeNull();
    expect(result[1].verifySkipReason).toBeNull();
  });

  it('emits totalTokens=null and totalCostUsd=null for pre-migration-31 rows', () => {
    // Rows whose agent ran before migration 31 (or whose phases all
    // crashed before producing a JSON envelope) have NULL metering —
    // the UI renders "no cost data" rather than a misleading $0.00.
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: 'Pre-migration agent',
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: 2824,
        total_tokens: null,
        total_cost_usd: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].totalTokens).toBeNull();
    expect(result[0].totalCostUsd).toBeNull();
  });

  it('emits issueTitle=null for rows with NULL/empty issue_title (pre-migration-28)', () => {
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: null,
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: null,
      },
      {
        agent_id: 'uuid-2',
        issue_number: 101,
        issue_title: '',
        status: 'failed',
        ended_at: '2026-04-18T13:00:00Z',
        pr_number: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].issueTitle).toBeNull();
    expect(result[1].issueTitle).toBeNull();
  });

  it('does NOT call GitHub — no async fetch needed', () => {
    // The function is synchronous — proves no hidden fetch path.
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: 'Title',
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    // Synchronous return — not a Promise.
    expect(Array.isArray(result)).toBe(true);
  });

  it('passes plan_blocked status through unchanged (#2857)', () => {
    // plan_blocked is a new terminal status parallel to failed/crashed;
    // the mapper must pass it through so the admin cockpit's
    // OutcomePill can render it with its distinct chip/colour.
    const rows = [
      {
        agent_id: 'uuid-planblocked',
        issue_number: 2857,
        issue_title: 'Plan correctly declined',
        status: 'plan_blocked',
        ended_at: '2026-04-19T20:00:00Z',
        pr_number: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result).toHaveLength(1);
    expect(result[0].status).toBe('plan_blocked');
    expect(result[0].agentId).toBe('uuid-planblocked');
  });

  it('passes needs_review status through unchanged (#2856)', () => {
    // needs_review is the "ralph produced SHIP code but summary
    // flagged unmet AC" terminal — parallel to plan_blocked but
    // operator-actionable (amber chip, not neutral). The mapper
    // must pass it through with its prNumber intact because the
    // whole point of the terminal is that the daemon opened a draft
    // PR; dropping prNumber would hide the draft from the cockpit.
    const rows = [
      {
        agent_id: 'uuid-needsreview',
        issue_number: 2856,
        issue_title: 'Summary flagged unmet AC',
        status: 'needs_review',
        ended_at: '2026-04-19T23:30:00Z',
        pr_number: 9001,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result).toHaveLength(1);
    expect(result[0].status).toBe('needs_review');
    expect(result[0].agentId).toBe('uuid-needsreview');
    expect(result[0].prNumber).toBe(9001);
  });
});

describe('parse-labels helpers (pure, no fetch)', () => {
  it('extractPriority picks the first priority/pN label', () => {
    expect(extractPriority(['area/api', 'priority/p1', 'agent/ready'])).toBe(
      'p1',
    );
    expect(extractPriority(['priority/p0'])).toBe('p0');
  });

  it('extractPriority returns null when no priority label is present', () => {
    expect(extractPriority(['area/api'])).toBeNull();
    expect(extractPriority([])).toBeNull();
    expect(extractPriority(null)).toBeNull();
    expect(extractPriority(undefined)).toBeNull();
  });

  it('extractPriority rejects malformed priority labels', () => {
    expect(extractPriority(['priority/p9'])).toBeNull();
    expect(extractPriority(['priority/high'])).toBeNull();
  });

  it('parseBlockedBy extracts every Blocked by #N line', () => {
    expect(parseBlockedBy(null)).toEqual([]);
    expect(parseBlockedBy(undefined)).toEqual([]);
    expect(parseBlockedBy('')).toEqual([]);
    expect(parseBlockedBy('Blocked by #1234\nSome prose.\nBlocked by #5678')).toEqual([
      1234, 5678,
    ]);
  });

  it('parseBlockedBy ignores "Parent:" lines (distinct mechanic)', () => {
    expect(parseBlockedBy('Parent: #1\n')).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// priorityRank + sortAndSliceQueueReady (issue #2843 — parallel to #2835)
// ---------------------------------------------------------------------------

describe('priorityRank', () => {
  it('maps priority labels to their rank (p0..p3 → 0..3)', () => {
    expect(priorityRank(['priority/p0'])).toBe(0);
    expect(priorityRank(['priority/p1'])).toBe(1);
    expect(priorityRank(['priority/p2'])).toBe(2);
    expect(priorityRank(['priority/p3'])).toBe(3);
  });

  it('returns the no-label floor (4) when no priority label is present', () => {
    expect(priorityRank(['area/api', 'type/bug'])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank([])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(PRIORITY_RANK_NO_LABEL).toBe(4);
  });

  it('returns the no-label floor for null / undefined / non-array input', () => {
    expect(priorityRank(null)).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(undefined)).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank('priority/p0')).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank({ 'priority/p0': 1 })).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(42)).toBe(PRIORITY_RANK_NO_LABEL);
  });

  it('ignores non-string entries in the label array', () => {
    // Every entry is non-string → floor
    expect(priorityRank([{ name: 'priority/p0' }, 42, null])).toBe(
      PRIORITY_RANK_NO_LABEL,
    );
    // Mixed — valid string wins despite noise
    expect(priorityRank([null, 'priority/p1', { obj: true }])).toBe(1);
  });

  it('picks the lowest rank when multiple priority labels are present', () => {
    expect(priorityRank(['priority/p2', 'priority/p0'])).toBe(0);
    expect(priorityRank(['priority/p3', 'priority/p1'])).toBe(1);
    expect(priorityRank(['priority/p2', 'priority/p3'])).toBe(2);
  });

  it('rejects malformed priority labels (priority/p9, priority/high, etc.)', () => {
    expect(priorityRank(['priority/p9'])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(['priority/high'])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(['priority/p0 '])).toBe(PRIORITY_RANK_NO_LABEL);
  });

  it('documents the end-to-end rank ordering (p0 < p1 < p2 < p3 < floor)', () => {
    const ranks = [
      priorityRank(['priority/p0']),
      priorityRank(['priority/p1']),
      priorityRank(['priority/p2']),
      priorityRank(['priority/p3']),
      priorityRank([]),
    ];
    expect(ranks).toEqual([0, 1, 2, 3, PRIORITY_RANK_NO_LABEL]);
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
  });
});

describe('sortAndSliceQueueReady', () => {
  /** Small helper — build a SnapshotIssueRecord with sensible defaults. */
  function issue(
    number: number,
    labels: string[],
    createdAt: string,
  ): SnapshotIssueRecord {
    return { number, title: `Issue ${number}`, labels, createdAt };
  }

  it('surfaces the p0 at row 1 even when it is the 23rd newest (#2712 scenario)', () => {
    // Mirrors the real-world case from the issue body: 22 p1/p2 issues
    // filed AFTER the single p0, stored in gh CREATED_AT DESC order.
    // With the buggy slice-only behaviour the p0 is invisible; the
    // fixed resolver must surface it at row 1.
    const newerP1P2: SnapshotIssueRecord[] = [];
    for (let i = 0; i < 22; i += 1) {
      // createdAt strictly newer than the p0 below. Alternate p1/p2.
      const day = String(20 - (i % 20)).padStart(2, '0');
      const createdAt = `2026-05-${day}T12:00:00Z`;
      const prio = i % 2 === 0 ? 'priority/p1' : 'priority/p2';
      newerP1P2.push(issue(3000 + i, [prio, 'agent/ready'], createdAt));
    }
    const p0 = issue(
      2712,
      ['priority/p0', 'agent/ready'],
      '2026-03-01T00:00:00Z',
    );
    // Storage order is CREATED_AT DESC — the 22 newer ones first, then the p0.
    const storedOrder = [...newerP1P2, p0];
    // Sanity — p0 is at the 23rd position by default.
    expect(storedOrder[22].number).toBe(2712);
    // The buggy path would drop the p0 from slice(0, 10) entirely.
    expect(storedOrder.slice(0, 10).some((i) => i.number === 2712)).toBe(false);

    const result = sortAndSliceQueueReady(storedOrder, 10);

    expect(result).toHaveLength(10);
    expect(result[0].number).toBe(2712);
    expect(priorityRank(result[0].labels)).toBe(0);
    // Every other row in top 10 is p1 (p1 beats p2 in the sort).
    for (let i = 1; i < result.length; i += 1) {
      expect(result[i].labels).toContain('priority/p1');
    }
  });

  it('sorts unlabelled issues last among open issues', () => {
    const unlabelled = issue(900, ['area/api'], '2026-01-01T00:00:00Z');
    const p3 = issue(901, ['priority/p3'], '2026-04-19T00:00:00Z');
    const p0 = issue(902, ['priority/p0'], '2026-04-19T00:00:00Z');
    const input = [unlabelled, p0, p3];

    const result = sortAndSliceQueueReady(input, 10);

    expect(result.map((r) => r.number)).toEqual([902, 901, 900]);
    // Specifically: the oldest-by-createdAt `unlabelled` still sorts last
    // because its rank (4) beats every priority-labelled issue.
    expect(result[result.length - 1].number).toBe(900);
  });

  it('breaks ties within a priority bucket by createdAt ASC (older first)', () => {
    // Three p1 issues, stored in createdAt DESC order (newest first).
    const newer = issue(100, ['priority/p1'], '2026-04-19T12:00:00Z');
    const middle = issue(101, ['priority/p1'], '2026-04-18T12:00:00Z');
    const older = issue(102, ['priority/p1'], '2026-04-17T12:00:00Z');

    const result = sortAndSliceQueueReady([newer, middle, older], 10);

    expect(result.map((r) => r.number)).toEqual([102, 101, 100]);
  });

  it('respects both keys: priority first, then createdAt ASC', () => {
    // Mixed priorities + mixed createdAts. Expected order:
    //   p0 (any createdAt wins), then p1 by createdAt ASC, then p2.
    const p0New = issue(1, ['priority/p0'], '2026-04-19T00:00:00Z');
    const p1Old = issue(2, ['priority/p1'], '2026-01-01T00:00:00Z');
    const p1New = issue(3, ['priority/p1'], '2026-04-19T00:00:00Z');
    const p2 = issue(4, ['priority/p2'], '2025-12-01T00:00:00Z');

    const result = sortAndSliceQueueReady([p2, p1New, p0New, p1Old], 10);

    expect(result.map((r) => r.number)).toEqual([1, 2, 3, 4]);
  });

  it('slices to `limit` after sorting, not before', () => {
    // Deliberately order the input so a naive `input.slice(0, limit)`
    // would drop the p0, exactly as the old queryQueueReady bug did.
    const input: SnapshotIssueRecord[] = [];
    for (let i = 0; i < 15; i += 1) {
      input.push(
        issue(2000 + i, ['priority/p2'], `2026-04-${String(19 - i).padStart(2, '0')}T00:00:00Z`),
      );
    }
    const p0 = issue(1, ['priority/p0'], '2025-01-01T00:00:00Z');
    input.push(p0); // p0 at position 15 (past the slice boundary)

    const result = sortAndSliceQueueReady(input, 10);

    expect(result).toHaveLength(10);
    expect(result[0].number).toBe(1);
  });

  it('returns an empty array for an empty input', () => {
    expect(sortAndSliceQueueReady([], 10)).toEqual([]);
  });

  it('does not mutate the input array', () => {
    const input: SnapshotIssueRecord[] = [
      issue(1, ['priority/p2'], '2026-04-19T00:00:00Z'),
      issue(2, ['priority/p0'], '2026-04-18T00:00:00Z'),
    ];
    const inputCopy = input.slice();

    sortAndSliceQueueReady(input, 10);

    expect(input).toEqual(inputCopy);
    expect(input[0].number).toBe(1);
  });

  it('handles missing / empty createdAt by sorting them last within their bucket', () => {
    const withTs = issue(1, ['priority/p1'], '2026-04-19T00:00:00Z');
    const withoutTs = issue(2, ['priority/p1'], '');

    const result = sortAndSliceQueueReady([withoutTs, withTs], 10);

    expect(result.map((r) => r.number)).toEqual([1, 2]);
  });

  it('handles a limit larger than the input length without padding', () => {
    const input: SnapshotIssueRecord[] = [
      issue(1, ['priority/p0'], '2026-04-19T00:00:00Z'),
      issue(2, ['priority/p1'], '2026-04-19T00:00:00Z'),
    ];

    const result = sortAndSliceQueueReady(input, 10);

    expect(result).toHaveLength(2);
    expect(result.map((r) => r.number)).toEqual([1, 2]);
  });
});

// ---------------------------------------------------------------------------
// sortAndSliceQueueBlocked (issue #2930 — mirror of #2843 for the
// admin cockpit's Queue: Blocked panel). Uses the same
// `(priorityRank ASC, createdAt ASC)` comparator as the ready queue
// so the operator reads both panels with the same mental model.
// ---------------------------------------------------------------------------

describe('sortAndSliceQueueBlocked', () => {
  /** Small helper — build a SnapshotIssueRecord with sensible defaults. */
  function issue(
    number: number,
    labels: string[],
    createdAt: string,
    body?: string,
  ): SnapshotIssueRecord {
    const rec: SnapshotIssueRecord = {
      number,
      title: `Issue ${number}`,
      labels,
      createdAt,
    };
    if (body !== undefined) rec.body = body;
    return rec;
  }

  it('sorts blocked issues by priority first (p1 before p2 before p3)', () => {
    // Storage order is what the daemon writes to
    // dispatcher.blocked_snapshots — unsorted w.r.t. priority. Before
    // #2930 this came out as createdAt DESC, so the p1 at the bottom
    // was buried beneath newer p2/p3 rows.
    const p3 = issue(
      100,
      ['priority/p3', 'status/blocked'],
      '2026-04-19T00:00:00Z',
      'Blocked by #1',
    );
    const p2 = issue(
      101,
      ['priority/p2', 'status/blocked'],
      '2026-04-18T00:00:00Z',
      'Blocked by #2',
    );
    const p1 = issue(
      102,
      ['priority/p1', 'status/blocked'],
      '2026-04-10T00:00:00Z',
      'Blocked by #3',
    );

    const result = sortAndSliceQueueBlocked([p3, p2, p1], 10);

    expect(result.map((r) => r.number)).toEqual([102, 101, 100]);
  });

  it('breaks ties within a priority bucket by createdAt ASC (longest-blocked surfaces)', () => {
    // Three p1-blocked issues — operator wants the longest-blocked
    // (oldest createdAt) first so investigation-aging is visible.
    const newer = issue(
      200,
      ['priority/p1', 'status/blocked'],
      '2026-04-19T12:00:00Z',
    );
    const middle = issue(
      201,
      ['priority/p1', 'status/blocked'],
      '2026-04-18T12:00:00Z',
    );
    const older = issue(
      202,
      ['priority/p1', 'status/blocked'],
      '2026-04-17T12:00:00Z',
    );

    const result = sortAndSliceQueueBlocked([newer, middle, older], 10);

    expect(result.map((r) => r.number)).toEqual([202, 201, 200]);
  });

  it('respects both keys: priority first, then createdAt ASC', () => {
    const p0 = issue(1, ['priority/p0', 'status/blocked'], '2026-04-19T00:00:00Z');
    const p1Old = issue(2, ['priority/p1', 'status/blocked'], '2026-01-01T00:00:00Z');
    const p1New = issue(3, ['priority/p1', 'status/blocked'], '2026-04-19T00:00:00Z');
    const p2 = issue(4, ['priority/p2', 'status/blocked'], '2025-12-01T00:00:00Z');

    const result = sortAndSliceQueueBlocked([p2, p1New, p0, p1Old], 10);

    expect(result.map((r) => r.number)).toEqual([1, 2, 3, 4]);
  });

  it('sorts unlabelled blocked issues last among open rows', () => {
    const unlabelled = issue(
      900,
      ['status/blocked'],
      '2026-01-01T00:00:00Z',
    );
    const p3 = issue(
      901,
      ['priority/p3', 'status/blocked'],
      '2026-04-19T00:00:00Z',
    );
    const p0 = issue(
      902,
      ['priority/p0', 'status/blocked'],
      '2026-04-19T00:00:00Z',
    );

    const result = sortAndSliceQueueBlocked([unlabelled, p0, p3], 10);

    expect(result.map((r) => r.number)).toEqual([902, 901, 900]);
  });

  it('slices to `limit` after sorting, not before', () => {
    // If a naive slice(0, limit) ran before the sort, the p1 at the
    // end of the input would drop off even though it should lead the
    // blocked panel.
    const input: SnapshotIssueRecord[] = [];
    for (let i = 0; i < 15; i += 1) {
      input.push(
        issue(
          2000 + i,
          ['priority/p3', 'status/blocked'],
          `2026-04-${String(19 - i).padStart(2, '0')}T00:00:00Z`,
        ),
      );
    }
    const p1 = issue(
      1,
      ['priority/p1', 'status/blocked'],
      '2025-01-01T00:00:00Z',
    );
    input.push(p1); // past the slice boundary

    const result = sortAndSliceQueueBlocked(input, 10);

    expect(result).toHaveLength(10);
    expect(result[0].number).toBe(1);
  });

  it('returns an empty array for an empty input', () => {
    expect(sortAndSliceQueueBlocked([], 10)).toEqual([]);
  });

  it('does not mutate the input array', () => {
    const input: SnapshotIssueRecord[] = [
      issue(1, ['priority/p2', 'status/blocked'], '2026-04-19T00:00:00Z'),
      issue(2, ['priority/p0', 'status/blocked'], '2026-04-18T00:00:00Z'),
    ];
    const inputCopy = input.slice();

    sortAndSliceQueueBlocked(input, 10);

    expect(input).toEqual(inputCopy);
    expect(input[0].number).toBe(1);
  });

  it('handles missing / empty createdAt by sorting them last within their bucket', () => {
    const withTs = issue(
      1,
      ['priority/p1', 'status/blocked'],
      '2026-04-19T00:00:00Z',
    );
    const withoutTs = issue(2, ['priority/p1', 'status/blocked'], '');

    const result = sortAndSliceQueueBlocked([withoutTs, withTs], 10);

    expect(result.map((r) => r.number)).toEqual([1, 2]);
  });

  it('preserves the snapshot body so parseBlockedBy can read it downstream', () => {
    // Unlike the ready queue, the blocked-queue snapshot carries
    // ``body`` (daemon writes it on blocked-scan only). The sort
    // helper must not strip or mutate it — queueItemFromSnapshot
    // parses it for the inline blocker list in the admin cockpit.
    const p1 = issue(
      10,
      ['priority/p1', 'status/blocked'],
      '2026-04-19T00:00:00Z',
      'Blocked by #5\nBlocked by #6',
    );
    const p2 = issue(
      11,
      ['priority/p2', 'status/blocked'],
      '2026-04-18T00:00:00Z',
      'Blocked by #7',
    );

    const result = sortAndSliceQueueBlocked([p2, p1], 10);

    expect(result.map((r) => r.number)).toEqual([10, 11]);
    expect(result[0].body).toBe('Blocked by #5\nBlocked by #6');
    expect(result[1].body).toBe('Blocked by #7');
  });

  it('surfaces a lone p1 ahead of newer p2/p3 blockers (#2930 ergonomics)', () => {
    // Real-world shape of the blocked queue: a couple of urgent
    // blockers buried under a pile of lower-priority ones. Before
    // #2930 the operator had to scan the whole list; after #2930
    // the p1s lead.
    const newerLower: SnapshotIssueRecord[] = [];
    // 18 p2s (enough to fill rows 2-10 without bleeding into p3) plus
    // a handful of p3s to stress the tie-break. Every row's createdAt
    // is strictly newer than the lone p1 below, so createdAt-DESC
    // (the pre-#2930 order) would bury the p1 at the tail of the list.
    for (let i = 0; i < 18; i += 1) {
      const day = String(20 - (i % 20)).padStart(2, '0');
      const createdAt = `2026-05-${day}T12:00:00Z`;
      newerLower.push(
        issue(
          3000 + i,
          ['priority/p2', 'status/blocked'],
          createdAt,
          'Blocked by #1',
        ),
      );
    }
    for (let i = 0; i < 4; i += 1) {
      const day = String(20 - (i % 20)).padStart(2, '0');
      const createdAt = `2026-05-${day}T12:00:00Z`;
      newerLower.push(
        issue(
          4000 + i,
          ['priority/p3', 'status/blocked'],
          createdAt,
          'Blocked by #1',
        ),
      );
    }
    const p1 = issue(
      2600,
      ['priority/p1', 'status/blocked'],
      '2026-03-01T00:00:00Z',
      'Blocked by #1',
    );
    // Storage order — createdAt DESC would keep p1 at the very end.
    const storedOrder = [...newerLower, p1];

    const result = sortAndSliceQueueBlocked(storedOrder, 10);

    expect(result).toHaveLength(10);
    expect(result[0].number).toBe(2600);
    expect(priorityRank(result[0].labels)).toBe(1);
    // Rows 2-10 must all be p2 (p2 beats p3 in the tie-break, and we
    // seeded 18 p2s — enough to fill the remaining nine slots without
    // spilling into p3).
    for (let i = 1; i < result.length; i += 1) {
      expect(result[i].labels).toContain('priority/p2');
    }
  });
});

// ---------------------------------------------------------------------------
// Metering helpers (#2869) — coerceNullableNumber, sumPhaseCost,
// phaseCostRowsToGraphQL. These back the new DispatcherAgent fields
// ``totalTokensInput``, ``totalTokensOutput``, ``totalCostUsd``, and
// ``phaseCostBreakdown``.
// ---------------------------------------------------------------------------

describe('coerceNullableNumber (#2869)', () => {
  it('returns the value unchanged for finite numbers', () => {
    expect(coerceNullableNumber(42)).toBe(42);
    expect(coerceNullableNumber(0)).toBe(0);
    expect(coerceNullableNumber(-3.14)).toBe(-3.14);
  });

  it('coerces pg numeric strings (cost_usd is returned as a string)', () => {
    // pg's default numeric→string mapping preserves precision. The
    // resolver must coerce to Number so the GraphQL scalar serializes
    // as a JSON number, not a quoted string.
    expect(coerceNullableNumber('0.0420')).toBe(0.042);
    expect(coerceNullableNumber('12345')).toBe(12345);
  });

  it('coerces bigint to Number', () => {
    // Token counts fit in 53 bits well under any plausible phase usage;
    // bigint → Number is safe.
    expect(coerceNullableNumber(12345n)).toBe(12345);
  });

  it('returns null for null / undefined / unparseable', () => {
    expect(coerceNullableNumber(null)).toBeNull();
    expect(coerceNullableNumber(undefined)).toBeNull();
    expect(coerceNullableNumber('nope')).toBeNull();
    expect(coerceNullableNumber({})).toBeNull();
    expect(coerceNullableNumber(NaN)).toBeNull();
  });
});

describe('sumPhaseCost (#2869)', () => {
  it('sums a column across rows, ignoring nulls', () => {
    const rows = [
      { tokens_input: 100, tokens_output: 50, cost_usd: '0.01' },
      { tokens_input: 200, tokens_output: null, cost_usd: '0.02' },
      { tokens_input: null, tokens_output: 30, cost_usd: null },
    ];
    expect(sumPhaseCost(rows, 'tokens_input')).toBe(300);
    expect(sumPhaseCost(rows, 'tokens_output')).toBe(80);
    expect(sumPhaseCost(rows, 'cost_usd')).toBeCloseTo(0.03, 10);
  });

  it('returns null when every row is null (no metering signal)', () => {
    // Distinct from returning 0 — the UI must be able to distinguish
    // "this agent actually spent $0" from "we have no data for this
    // agent". Pre-migration-31 agents all have NULL across the board.
    const rows = [
      { tokens_input: null, tokens_output: null, cost_usd: null },
      { tokens_input: null, tokens_output: null, cost_usd: null },
    ];
    expect(sumPhaseCost(rows, 'tokens_input')).toBeNull();
    expect(sumPhaseCost(rows, 'tokens_output')).toBeNull();
    expect(sumPhaseCost(rows, 'cost_usd')).toBeNull();
  });

  it('returns 0 when at least one row has a zero value (distinct from null)', () => {
    const rows = [
      { tokens_input: 0, tokens_output: null, cost_usd: '0' },
    ];
    expect(sumPhaseCost(rows, 'tokens_input')).toBe(0);
    // tokens_output is entirely null → null sentinel.
    expect(sumPhaseCost(rows, 'tokens_output')).toBeNull();
    expect(sumPhaseCost(rows, 'cost_usd')).toBe(0);
  });

  it('returns null for an empty row list', () => {
    expect(sumPhaseCost([], 'tokens_input')).toBeNull();
    expect(sumPhaseCost([], 'cost_usd')).toBeNull();
  });
});

describe('phaseCostRowsToGraphQL (#2869)', () => {
  it('maps snake_case columns to camelCase GraphQL fields with nullable Number coercion', () => {
    const rows = [
      {
        phase: 'plan',
        tokens_input: '1000',
        tokens_output: '200',
        tokens_cache_read: '3000',
        tokens_cache_write: '400',
        cost_usd: '0.0123',
        model_used: 'claude-opus-4-5',
      },
      {
        phase: 'ralph',
        tokens_input: null,
        tokens_output: null,
        tokens_cache_read: null,
        tokens_cache_write: null,
        cost_usd: null,
        model_used: null,
      },
    ];
    expect(phaseCostRowsToGraphQL(rows)).toEqual([
      {
        phase: 'plan',
        tokensInput: 1000,
        tokensOutput: 200,
        tokensCacheRead: 3000,
        tokensCacheWrite: 400,
        costUsd: 0.0123,
        modelUsed: 'claude-opus-4-5',
      },
      {
        phase: 'ralph',
        tokensInput: null,
        tokensOutput: null,
        tokensCacheRead: null,
        tokensCacheWrite: null,
        costUsd: null,
        modelUsed: null,
      },
    ]);
  });

  it('returns an empty array for an empty row list', () => {
    expect(phaseCostRowsToGraphQL([])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Category display-name rephrasing (issue #2948)
// ---------------------------------------------------------------------------

describe('CATEGORY_DISPLAY_NAMES (#2948 — Recent Failures table rephrase)', () => {
  it('locks the full map so a typo or accidental deletion fails the test', () => {
    // Must stay in sync with DispatcherDaemon._CATEGORY_DISPLAY_NAMES
    // and DispatcherDaemon._NO_TAIL_CATEGORY_SUMMARIES in
    // scripts/dispatcher/daemon.py. If a new category is added on the
    // Python side, adding its LHS here (and updating this assertion) is
    // part of the rephrase. If the Python map changes but this one
    // doesn't, the Recent Failures table falls back to the raw token
    // for the new category — acceptable degradation, but the mismatch
    // is surfaced by the Python side's
    // ``test_category_display_names_map_contents`` test.
    expect(CATEGORY_DISPLAY_NAMES).toEqual({
      subprocess_turn_limit: 'turn limit reached',
      subprocess_crash: 'subprocess crashed',
      subprocess_auth_fail: 'auth failed',
      ci_red_after_retries: 'CI failed after retries',
      gh_rate_exhausted: 'GitHub rate limit',
      stuck_timeout: 'timed out',
      paused_by_killswitch: 'manually stopped',
      daemon_restart_abandoned: 'dispatcher restarted',
    });
  });

  it('displayCategoryFor rephrases known tokens', () => {
    expect(displayCategoryFor('subprocess_turn_limit')).toBe('turn limit reached');
    expect(displayCategoryFor('daemon_restart_abandoned')).toBe('dispatcher restarted');
    expect(displayCategoryFor('paused_by_killswitch')).toBe('manually stopped');
    expect(displayCategoryFor('ci_red_after_retries')).toBe('CI failed after retries');
  });

  it('displayCategoryFor falls through to the raw token for unknown categories', () => {
    // Acceptance criterion #3: unknown category (future additions not
    // yet in the map) falls through to the raw token as today.
    expect(displayCategoryFor('some_new_category')).toBe('some_new_category');
    expect(displayCategoryFor('')).toBe('');
  });
});

describe('failureRowToGraphQL (#2948 — displayCategory field)', () => {
  it('exposes displayCategory alongside the raw category token', () => {
    const row = {
      failure_id: 'failure-1',
      agent_id: 'agent-1',
      category: 'subprocess_turn_limit',
      detected_by: 'supervisor:turn_limit',
      details: { note: 'ran out of turns' },
      ts: '2026-04-20T12:00:00Z',
      issue_number: 2948,
    };
    expect(failureRowToGraphQL(row)).toEqual({
      failureId: 'failure-1',
      agentId: 'agent-1',
      category: 'subprocess_turn_limit',
      displayCategory: 'turn limit reached',
      detectedBy: 'supervisor:turn_limit',
      details: { note: 'ran out of turns' },
      ts: '2026-04-20T12:00:00Z',
      issueNumber: 2948,
    });
  });

  it('passes unknown categories through to displayCategory verbatim', () => {
    const row = {
      failure_id: 'failure-2',
      agent_id: null,
      category: 'some_future_category',
      detected_by: 'hook:unknown',
      details: null,
      ts: '2026-04-20T12:00:00Z',
      issue_number: null,
    };
    const out = failureRowToGraphQL(row);
    expect(out.category).toBe('some_future_category');
    expect(out.displayCategory).toBe('some_future_category');
    // null details round-trips to an empty object (consistent with
    // pre-#2948 behavior — `details: JSON!` is non-nullable on the
    // schema).
    expect(out.details).toEqual({});
    expect(out.issueNumber).toBeNull();
  });

  it('rephrases the display-only alias categories (_NO_TAIL_CATEGORY_SUMMARIES)', () => {
    // paused_by_killswitch and daemon_restart_abandoned come from
    // Python's _NO_TAIL_CATEGORY_SUMMARIES map, not the main
    // _CATEGORY_DISPLAY_NAMES map — but they still appear as raw
    // category tokens in dispatcher.failures rows, so the Recent
    // Failures table needs to rephrase them too (issue #2948).
    expect(
      failureRowToGraphQL({
        failure_id: 'f1',
        agent_id: 'a1',
        category: 'paused_by_killswitch',
        detected_by: 'supervisor:killswitch',
        details: {},
        ts: '2026-04-20T12:00:00Z',
        issue_number: null,
      }).displayCategory,
    ).toBe('manually stopped');

    expect(
      failureRowToGraphQL({
        failure_id: 'f2',
        agent_id: 'a2',
        category: 'daemon_restart_abandoned',
        detected_by: 'supervisor:daemon_restart',
        details: {},
        ts: '2026-04-20T12:00:00Z',
        issue_number: null,
      }).displayCategory,
    ).toBe('dispatcher restarted');
  });

  it('coerces a non-string category to empty + empty displayCategory', () => {
    // Defensive — the resolver type-guards category before passing it
    // through. A malformed row (null category, which should never
    // happen under the NOT NULL constraint) should collapse cleanly.
    const out = failureRowToGraphQL({
      failure_id: 'f3',
      agent_id: null,
      category: null as unknown as string,
      detected_by: 'hook',
      details: {},
      ts: '2026-04-20T12:00:00Z',
      issue_number: null,
    });
    expect(out.category).toBe('');
    expect(out.displayCategory).toBe('');
  });
});
