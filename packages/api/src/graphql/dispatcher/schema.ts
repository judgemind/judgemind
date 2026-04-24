/**
 * GraphQL schema additions for the dispatcher v2 admin surface.
 *
 * Feeds the admin page at `/admin/dispatcher` (sub-task E) and any future
 * admin tooling that needs to read or nudge the daemon. See
 * `docs/specs/dispatcher-v2-spec.md` §11.
 *
 * All queries and mutations are gated on `users.role = 'admin'`. Non-admins
 * receive a generic "not found" error (no field-shape introspection of the
 * underlying tables — see §11 Auth).
 */

export const dispatcherTypeDefs = `#graphql
  # ---------------------------------------------------------------------------
  # Scalar shims — lightweight ISO-8601 / UUID / JSON wrappers implemented as
  # strings. Real scalar types (DateTime, UUID, JSON) live downstream; for now
  # these type aliases document intent for the admin page consumer.
  # ---------------------------------------------------------------------------

  """Base64-encoded ISO-8601 timestamp (e.g. "2026-04-18T19:00:00Z")."""
  scalar DateTime

  """Opaque JSON scalar — object shape varies by field."""
  scalar JSON

  # ---------------------------------------------------------------------------
  # Enum — admin control commands (#2884 simplified taxonomy)
  # ---------------------------------------------------------------------------

  """Control commands the admin page can issue to the daemon.
  Consumed via \`dispatcher.commands\` row insert.

  Four commands: three global daemon-state transitions
  (\`start\` / \`stop\` / \`force_stop\`) and one per-agent action
  (\`retry\`). Previously the surface included \`pause\`, \`resume\`,
  \`drain\`, and a separate \`force_kill\` — all removed per #2884.
  Stopping development work is not destructive, so no MFA gate.
  """
  enum DispatcherCommand {
    """Resume claiming; sets \`concurrency_cap\` from 0 back to 1."""
    start
    """Graceful stop: blocks new spawns and lets any in-flight agent
    finish its current phase pipeline. Sets \`concurrency_cap\` to 0
    without engaging the killswitch — the in-flight worker keeps
    running. Replaces the former \`drain\` command (same SQL, clearer
    semantic boundary vs. \`force_stop\`)."""
    stop
    """Immediate stop. Sets \`concurrency_cap\` to 0 AND engages the
    killswitch so the in-flight worker aborts at the next phase
    boundary via \`_check_killswitch_and_abort\`.

    When \`payload.agentId\` is supplied, scope narrows to that single
    agent: kill its subprocess (SIGKILL if the pid is local) and mark
    it \`status='crashed'\` without changing the global
    \`concurrency_cap\`. Replaces the former \`force_kill\` command."""
    force_stop
    """Queue a manual retry for a specific agent. \`payload.agentId\` required."""
    retry
  }

  # ---------------------------------------------------------------------------
  # Types
  # ---------------------------------------------------------------------------

  """One daemon boot. Latest row in \`dispatcher.runs\` is the active lease."""
  type DispatcherRun {
    runId: ID!
    startedAt: DateTime!
    stoppedAt: DateTime
    heartbeatTs: DateTime!
    versionSha: String!
    host: String!
    pid: Int!
  }

  """One phase transition for a given agent. Append-only log row."""
  type PhaseTransition {
    transitionId: ID!
    phase: String!
    ts: DateTime!
    autocompactCount: Int!
  }

  """One deterministic failure detection (hook or supervisor-written)."""
  type DispatcherFailure {
    failureId: ID!
    agentId: ID
    """Stored machine-readable category token (e.g.
    \`subprocess_turn_limit\`, \`daemon_restart_abandoned\`). Used by
    CloudWatch queries, SQL filters, and retry classification — keep
    this field available for operator tooling even when surfacing
    \`displayCategory\` in the UI."""
    category: String!
    """Operator-friendly rephrasing of \`category\` (e.g.
    \`subprocess_turn_limit\` → \`turn limit reached\`). Computed
    server-side from the display-name map in
    packages/api/src/graphql/dispatcher/resolvers.ts (which mirrors
    DispatcherDaemon._CATEGORY_DISPLAY_NAMES and
    _NO_TAIL_CATEGORY_SUMMARIES in scripts/dispatcher/daemon.py).
    Categories not in the map fall through to the raw token
    verbatim. Issue #2948 — keeps the admin cockpit's Recent
    Failures table consistent with the Recently Completed tooltips
    introduced in #2935/#2939."""
    displayCategory: String!
    detectedBy: String!
    """Opaque category-specific JSON payload."""
    details: JSON!
    ts: DateTime!
    """Issue number the failure is associated with (derived from agents row if present)."""
    issueNumber: Int
  }

  """One /task (or audit/spotcheck) agent invocation.

  Post-#2927 every row is daemon-owned — the /task skill no longer
  writes to \`dispatcher.agents\` (label-only coordination). The
  underlying \`kind\` column is preserved in the DB for backward
  compatibility with historical rows but is not exposed on the
  GraphQL surface.
  """
  type DispatcherAgent {
    id: ID!
    issueNumber: Int!
    """Issue title captured at claim time by the daemon's
    \`_atomic_claim\` from the queue-snapshot enrichment, issue #2820.
    Null when the issue is not in the snapshot or the row predates
    migration 28 — the admin cockpit falls back to \`#<number>\`.
    Exposed on the active-agents query so the unified table can render
    title alongside issue+priority without a separate GitHub
    round-trip. Issue #2899."""
    issueTitle: String
    """Priority label captured at claim time — one of p0 | p1 | p2 | p3 | null.
    Written by the daemon's \`_atomic_claim\`. Reflects "priority when
    spawned"; does NOT retroactively update when the issue is relabeled
    on GitHub after the claim. Pre-migration-33 rows return null —
    the admin cockpit renders those as an em-dash placeholder.
    Issue #2899."""
    priority: String
    worktreePath: String!
    phase: String!
    """One of running | succeeded | failed | retrying | crashed | plan_blocked | needs_review.

    Correct-outcome terminals (distinct from \`failed\` which is
    reserved for genuine infrastructure/subprocess failures):

    - \`plan_blocked\` (#2857) — plan phase correctly declined to proceed
      (malformed issue, missing info, etc.). Admin cockpit renders with
      a neutral muted chip — operator-informational, not alarming.
    - \`needs_review\` (#2856) — ralph completed with verdict=SHIP but
      the summary phase flagged unmet acceptance criteria. The daemon
      opened a DRAFT PR preserving ralph's work; operator reviews, marks
      ready + merges, closes, or iterates. Admin cockpit renders with
      an amber/yellow chip — actionable, needs operator eyes.

    Kept as \`String!\` rather than a GraphQL enum so future
    correct-outcome terminals slot in without a schema migration
    (\`dispatcher.agents.status\` is plain \`text\` in the DB — see
    migration 25, no CHECK constraint).
    """
    status: String!
    startedAt: DateTime!
    endedAt: DateTime
    exitCode: Int
    prNumber: Int
    retriesUsed: Int!
    """Ordered ascending by transition ts."""
    phaseTransitions: [PhaseTransition!]!
    """Failures linked to this agent, newest first."""
    failures: [DispatcherFailure!]!
    """Sum of \`tokens_input\` across every phase the agent ran (#2869).
    Null when no phase row has recorded usage (e.g. the agent ran before
    migration 31 landed, or every phase crashed before producing a JSON
    envelope)."""
    totalTokensInput: Float
    """Sum of \`tokens_output\` across every phase the agent ran (#2869).
    Null when no phase row has recorded usage."""
    totalTokensOutput: Float
    """Sum of \`cost_usd\` across every phase the agent ran (#2869). Null
    when no phase row has recorded usage.

    WARNING: this is Claude Code's list-price cost estimate, NOT the
    Max plan-adjusted actual-billed amount. Useful for relative
    run-to-run comparison, not for absolute spend accounting."""
    totalCostUsd: Float
    """Per-phase cost breakdown for this agent (#2869). One entry per
    phase that emitted a usage block. Empty list when no phases have
    recorded usage."""
    phaseCostBreakdown: [PhaseCost!]!
  }

  """Per-phase aggregated token + cost totals for one agent. One entry
  per phase that emitted a usage block from its \`claude -p
  --output-format json\` invocation (#2869).

  Summed across retry attempts (phase_outputs.attempt) so an agent that
  hit a tier-1 retry mid-run shows a single row per phase with the
  combined cost of both attempts — the operator cares about total spend
  per phase, not per-attempt."""
  type PhaseCost {
    """One of plan | ralph | summary | fix_ci | verify | retro."""
    phase: String!
    tokensInput: Float
    tokensOutput: Float
    tokensCacheRead: Float
    tokensCacheWrite: Float
    """Same list-price caveat as \`DispatcherAgent.totalCostUsd\`."""
    costUsd: Float
    """The resolved model name observed on the LATEST attempt (most
    recent \`ts\`). Distinct-per-phase because phase 3's \`model_by_phase\`
    config sets one model per phase; if a retry attempted a different
    model (operator override via \`model_override\`) the latest wins."""
    modelUsed: String
  }

  """One row in the queue side-panels (#2805 §1.3). Capped at 10 server-side.

  - \`queueReady\` entries are open issues labelled \`agent/ready\` + not
    assigned + not blocked. Sorted by priority asc then created_at asc.
  - \`queueBlocked\` entries are open issues labelled \`status/blocked\`.
    Sorted by created_at desc. \`blockedBy\` is parsed from the issue body's
    \`Blocked by #N\` lines.
  """
  type QueueItem {
    issueNumber: Int!
    title: String!
    """One of p0 | p1 | p2 | p3 | null (parsed from priority/pN label)."""
    priority: String
    labels: [String!]!
    createdAt: DateTime!
    """Issue numbers this issue is blocked by (from the 'Blocked by #N' lines
    in the body). Empty for queueReady items."""
    blockedBy: [Int!]!
    """Seconds left before this issue is eligible for the scheduler to pick up
    again after a recent failure. Null when no prior \`dispatcher.agents\` row
    exists or when cooldown has elapsed (0 = expired, positive = still cooling,
    null = never attempted). Issue #3001."""
    cooldownSecondsRemaining: Int
  }

  """One row in the 'Recently completed' panel (#2805 §1.5). Derived from
  \`dispatcher.agents\` where status IN
  ('succeeded','failed','crashed','plan_blocked','needs_review'), newest
  first. Capped at 10 server-side."""
  type RecentCompletion {
    """Agent id (UUID)."""
    agentId: ID!
    """Issue the agent was working on."""
    issueNumber: Int!
    """Priority label captured at claim time — one of p0 | p1 | p2 | p3 | null.
    Same semantics as \`DispatcherAgent.priority\`: reflects "priority
    when spawned". Pre-migration-33 rows return null (em-dash in the
    admin cockpit). Issue #2899."""
    priority: String
    """Issue title (fetched live from GitHub; null if the lookup failed)."""
    issueTitle: String
    """One of succeeded | failed | crashed | plan_blocked | needs_review.
    See \`DispatcherAgent.status\` for the semantics of each value."""
    status: String!
    """Timestamp the agent claimed the issue (\`dispatcher.agents.started_at\`).
    Always populated — \`started_at\` is non-nullable on the agents table.
    Issue #3024: paired with \`endedAt\` so the admin cockpit can render a
    Start/Duration/End tooltip on the relative-time cell in the Recently
    completed panel."""
    startedAt: DateTime!
    endedAt: DateTime!
    """PR number if the agent produced one; null otherwise. \`needs_review\`
    rows (#2856) always have \`prNumber\` populated because the whole
    point of the terminal is that the daemon opened a draft PR."""
    prNumber: Int
    """Sum of input+output tokens across every phase the agent ran
    (#2869). Null when no phase row has recorded usage. Rendered as a
    \`Nk tok\` footnote in the admin cockpit."""
    totalTokens: Float
    """Sum of \`cost_usd\` across every phase the agent ran (#2869).
    Null when no phase row has recorded usage. Rendered as a \`~$X.XX\`
    footnote in the admin cockpit.

    WARNING: list-price estimate, NOT Max plan-adjusted. See
    \`DispatcherAgent.totalCostUsd\`."""
    totalCostUsd: Float
    """Short "what happened" string for failure terminals (\`failed\`,
    \`crashed\`, \`plan_blocked\`). Populated at terminal-time by the
    daemon from \`failures.category\` + \`phase\` + stderr tail, then
    optionally upgraded by \`/diagnose-failure\` to a richer LLM-
    authored sentence. Capped at 240 chars. NULL for \`succeeded\` /
    \`needs_review\` rows and for historical rows that pre-date
    migration 33. Rendered as a tooltip on the outcome glyph in the
    admin cockpit's \`Recently completed\` panel. Issue #2900."""
    failureSummary: String
    """Timestamp the PR squash-merge was observed by the daemon. One-way
    latch — written at merge time, never cleared. Paired with the
    \`status='succeeded'\` flip that happens in the same write, so
    \`mergedAt != null\` is the canonical "shipped" signal (use it in
    preference to \`status\` for "did this PR make it"). NULL on rows
    that never merged (push_and_pr failed, CI red after retries, etc.)
    and on pre-migration-35 historical rows. Issue #2953."""
    mergedAt: DateTime
    """Timestamp the verify phase completed with verdict=VERIFIED. NULL
    when verify was intentionally skipped (see \`verifySkipReason\`),
    when verify crashed mid-phase, or for pre-migration-35 rows. A
    merged row with NULL \`verifiedAt\` AND NULL \`verifySkipReason\`
    renders as amber ✓ in the admin cockpit — "shipped but post-merge
    bookkeeping incomplete". Issue #2953."""
    verifiedAt: DateTime
    """Non-null when verify was intentionally skipped. Today the only
    written value is \`"self_deploy"\` (dispatcher-self-PR touches
    \`scripts/dispatcher/\`). Admin cockpit treats a merged row with
    a non-null skip reason as fully-shipped (green ✓) — skipping is
    not a regression signal. Issue #2953."""
    verifySkipReason: String
    """Timestamp the retro phase completed (reached PHASE_RETRO_DONE).
    NULL when retro crashed, when the worktree was already gone at
    retro time (fast-path to cleanup_done), or for pre-migration-35
    rows. Rendered as the third tick in the milestone-breakdown
    tooltip. Issue #2953."""
    retroedAt: DateTime
  }

  """One key/value entry from \`dispatcher.config\` (#2805 §1.6)."""
  type DispatcherConfigEntry {
    key: String!
    """JSON-encoded string — the on-disk jsonb \`value\` column.
    Clients parse this on read and send a JSON-encoded string on write."""
    value: String!
    updatedAt: DateTime!
    updatedBy: String!
  }

  """Result of \`dispatcherControl\` — the command row that was written (or an existing one, if idempotent hit)."""
  type DispatcherCommandResult {
    commandId: ID!
    command: DispatcherCommand!
    issuedBy: String!
    issuedAt: DateTime!
    consumedAt: DateTime
    payload: JSON!
    """True when this row was created by this call; false when an identical recent row was returned (idempotency)."""
    created: Boolean!
  }

  """Aggregate snapshot read by the admin page's polling loop (\`pollInterval: 2000\`)."""
  type DispatcherState {
    """Latest row in \`dispatcher.runs\`; null if the daemon has never booted."""
    currentRun: DispatcherRun
    """Rows in \`dispatcher.agents\` where status='running'."""
    activeAgents: [DispatcherAgent!]!
    """Failures from \`dispatcher.failures\` in the last \`sinceHours\` (default 24)."""
    recentFailures(sinceHours: Int = 24): [DispatcherFailure!]!
    """Count of open \`agent/ready\` issues. Sourced from the most recent row in
    \`dispatcher.queue_snapshots\`, written by the daemon on each 30s scheduler
    tick (Phase 2+). Returns 0 before the daemon has booted or when every
    recent scan has failed — the admin page treats that as "queue unknown / 0"."""
    queueDepth: Int!
    """Count of open \`status/blocked\` issues. Sourced from the most recent row
    in \`dispatcher.blocked_snapshots\`, written by the daemon on a slower
    cadence (every N scheduler ticks — blocked list changes slowly). Returns 0
    before the daemon has booted or when no blocked scan has landed — the admin
    page treats that as "0 blocked / unknown" just like \`queueDepth\`. Exposed
    alongside the capped \`queueBlocked\` list so the admin page header can
    render \`{shown} / {total}\` without losing the tail beyond the server-side
    cap of 10 (issue #2886)."""
    blockedDepth: Int!
    """Top 10 ready-for-pickup issues (#2805 §1.3). Sourced exclusively from
    the most recent \`dispatcher.queue_snapshots.issues_json\` row — the
    daemon pre-enriches title/labels/createdAt at scrape time so this
    resolver makes no GitHub API calls (issue #2820). Issues with an
    active agent are filtered out; the remaining items are sorted by
    priority then createdAt ASC and capped at 10. Empty list when the
    daemon has not yet written a snapshot. Use \`dispatcherQueueFull(kind: READY)\`
    to fetch the full list (no 10-cap) on demand for the cockpit's
    expand-count dialog (issue #3159)."""
    queueReady: [QueueItem!]!
    """Top 10 blocked issues (#2805 §1.3). Sourced exclusively from the
    most recent \`dispatcher.blocked_snapshots.issues_json\` row — same
    snapshot-only data path as \`queueReady\`, no GitHub API calls
    (issue #2820). Sorted by priority then createdAt ASC and capped at
    10. Empty list when nothing is blocked or when the daemon has not
    yet written a blocked snapshot. Use \`dispatcherQueueFull(kind: BLOCKED)\`
    to fetch the full list (no 10-cap) on demand for the cockpit's
    expand-count dialog (issue #3159)."""
    queueBlocked: [QueueItem!]!
    """Top 10 recently-completed agents (#2805 §1.5). Rows from
    \`dispatcher.agents\` where status IN
    ('succeeded','failed','crashed','plan_blocked','needs_review')
    ordered by \`ended_at\` DESC."""
    recentCompletions: [RecentCompletion!]!
    """Total count of terminal-status rows in \`dispatcher.agents\`
    (status IN \`('succeeded','failed','crashed','plan_blocked','needs_review')\`).
    Same filter \`recentCompletions\` (and \`dispatcherQueueFull(kind: COMPLETED)\`)
    use — paired with the capped \`recentCompletions\` list so the
    admin-cockpit panel header can render \`{shown} / {total}\`,
    matching the Ready/Blocked panels (issue #3172). \`dispatcher.agents\`
    is not currently in the daemon's housekeeping target list, so this
    grows unboundedly — well within the 1000-row dialog cap today
    (~180 rows on dev). Returns 0 when the table is empty (fresh deploy)."""
    recentCompletionsCount: Int!
    """All live-editable \`dispatcher.config\` entries (#2805 §1.6)."""
    config: [DispatcherConfigEntry!]!
    """\`dispatcher.config.value\` for \`spawn_frozen_until\` (§10), or null if not set."""
    spawnFrozenUntil: DateTime
    """True when the circuit breaker is open
    (\`dispatcher.config.cap_flipped_by\` == \`"circuit_breaker"\`).
    Surfaces the open state in the admin cockpit so the operator sees
    "Circuit open: N recent agents bad, cap held at 0" instead of a
    silent cap=0. Issue #2860."""
    circuitBreakerOpen: Boolean!
    """Diagnostic trail for the last \`concurrency_cap\` flip. One of
    \`"circuit_breaker"\` | \`"operator"\` | another identifier |
    \`null\` (never set). Read from \`dispatcher.config.cap_flipped_by\`.
    Issue #2860."""
    capFlippedBy: String
  }

  """Which queue bucket a \`dispatcherQueueFull\` lookup is for (issue #3159)."""
  enum DispatcherQueueKind {
    """Open \`agent/ready\` issues — fetched from
    \`dispatcher.queue_snapshots.issues_json\`. Same data path and ordering
    as \`DispatcherState.queueReady\`, just without the 10-cap."""
    READY
    """Open \`status/blocked\` issues — fetched from
    \`dispatcher.blocked_snapshots.issues_json\`. Same data path and ordering
    as \`DispatcherState.queueBlocked\`, just without the 10-cap."""
    BLOCKED
    """Recently terminal agents (\`succeeded | failed | crashed | plan_blocked | needs_review\`)
    — fetched from \`dispatcher.agents\`. Same query as
    \`DispatcherState.recentCompletions\`, just without the 10-cap."""
    COMPLETED
  }

  """Full-list payload for the cockpit's expand-count dialogs (issue #3159).
  Read on dialog open only — NOT in the 2s \`dispatcherState\` poll path.
  Reads exclusively from \`dispatcher.queue_snapshots.issues_json\` /
  \`dispatcher.blocked_snapshots.issues_json\` / \`dispatcher.agents\` —
  no GitHub API calls.

  Exactly one of \`queueItems\` or \`completions\` is populated per
  request, keyed by \`kind\`:
    - kind=READY / BLOCKED → \`queueItems\` populated, \`completions\` empty.
    - kind=COMPLETED → \`completions\` populated, \`queueItems\` empty.
  """
  type DispatcherQueueFull {
    """Echoes the \`kind\` argument so Apollo can normalize cache entries
    by bucket (\`keyFields: ['kind']\` on the client)."""
    kind: DispatcherQueueKind!
    """Every QueueItem in the snapshot for kind=READY / BLOCKED. Sorted by
    priority then createdAt ASC — same comparator as the capped
    \`DispatcherState.queueReady\` / \`queueBlocked\` panels. Empty when
    \`kind=COMPLETED\`."""
    queueItems: [QueueItem!]!
    """Every recent terminal agent for kind=COMPLETED. Ordered by
    \`endedAt\` DESC — same query as the capped
    \`DispatcherState.recentCompletions\` panel. Empty when
    \`kind=READY\` or \`kind=BLOCKED\`."""
    completions: [RecentCompletion!]!
  }

  # ---------------------------------------------------------------------------
  # Queries + Mutations — merged into the root schema by concatenation.
  # The root Query/Mutation types are open for extension via the
  # \`extend type\` keyword.
  # ---------------------------------------------------------------------------

  extend type Query {
    """Aggregate dispatcher snapshot. Admin-only; non-admins receive "not found"."""
    dispatcherState: DispatcherState!

    """Full detail for one dispatcher agent, including phase transitions and failures.
    Returns null when the agentId does not exist.
    Admin-only; non-admins receive "not found"."""
    dispatcherAgent(agentId: ID!): DispatcherAgent

    """Full list of one cockpit queue bucket (issue #3159). Used by the
    expand-count dialog on the dispatcher cockpit. Returns every item the
    daemon snapshot knows about — no 10-cap. Reads from the same snapshot
    tables as the capped \`DispatcherState\` fields; no GitHub API calls.
    Admin-only; non-admins receive "not found"."""
    dispatcherQueueFull(kind: DispatcherQueueKind!): DispatcherQueueFull!
  }

  extend type Mutation {
    """Issue an admin control command to the daemon. Writes a row to
    \`dispatcher.commands\` — the daemon consumes it on its next 30s tick.

    Idempotency: if an identical row (same command + issuedBy + payload) was
    written in the last 10 seconds and is still unconsumed, return that row
    and set \`created=false\` instead of inserting a duplicate.

    #2884: the MFA re-auth placeholder was removed. Admin session auth
    is sufficient; audit trail lives in \`dispatcher.commands.issued_by\`.

    Admin-only; non-admins receive "not found".
    """
    dispatcherControl(
      command: DispatcherCommand!
      """Command-specific JSON payload. For \`retry\` this must include
      \`agentId\`. For \`force_stop\`, \`agentId\` is optional — when
      present the command is scoped to that agent; when absent it is a
      global immediate stop. For \`start\` and \`stop\`, \`{}\` is fine."""
      payload: JSON
    ): DispatcherCommandResult!

    """Update a single \`dispatcher.config\` entry (#2805 §1.6). Writes the
    new value as JSONB, stamps \`updated_at\`, and records the admin's email
    in \`updated_by\` for audit.

    #2884: the MFA re-auth placeholder was removed — admin session auth
    is the gate.

    Admin-only; non-admins receive "not found".
    """
    dispatcherSetConfig(
      """The \`dispatcher.config.key\` to update. Must match an existing row."""
      key: String!
      """The new value, as a JSON-encoded string (e.g. \`"1"\` for a number,
      \`"\\"on\\""\` for a string, \`"[60,300]"\` for an array). The server
      validates the parsed value per-key — see resolver for the per-key
      constraints."""
      value: String!
    ): DispatcherConfigEntry!
  }
`;
