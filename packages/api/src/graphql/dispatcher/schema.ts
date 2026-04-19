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
  # Enum — admin control commands
  # ---------------------------------------------------------------------------

  """Control commands the admin page can issue to the daemon.
  Consumed via \`dispatcher.commands\` row insert.

  Destructive commands (\`stop\`, \`drain\`, \`force_kill\`) require fresh
  re-auth (MFA-style) per §17 Risk 6 — see \`dispatcherControl\`.
  """
  enum DispatcherCommand {
    """Resume scheduler + supervisor ticks after a pause/stop."""
    start
    """Block new spawns; let in-flight agents finish (destructive)."""
    stop
    """Block new spawns with aggressive timeout on in-flight agents (destructive)."""
    drain
    """Suspend both scheduler and supervisor until \`resume\`."""
    pause
    """Resume after \`pause\`."""
    resume
    """Queue a manual retry for a specific agent (payload.agentId required)."""
    retry
    """Kill a specific agent's subprocess without waiting (destructive; payload.agentId required)."""
    force_kill
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
    category: String!
    detectedBy: String!
    """Opaque category-specific JSON payload."""
    details: JSON!
    ts: DateTime!
    """Issue number the failure is associated with (derived from agents row if present)."""
    issueNumber: Int
  }

  """One /task (or audit/spotcheck) agent invocation."""
  type DispatcherAgent {
    id: ID!
    kind: String!
    issueNumber: Int!
    worktreePath: String!
    phase: String!
    """One of running | succeeded | failed | retrying | crashed."""
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
  }

  """One row in the 'Recently completed' panel (#2805 §1.5). Derived from
  \`dispatcher.agents\` where status IN ('succeeded','failed','crashed'),
  newest first. Capped at 10 server-side."""
  type RecentCompletion {
    """Agent id (UUID)."""
    agentId: ID!
    """Issue the agent was working on."""
    issueNumber: Int!
    """Issue title (fetched live from GitHub; null if the lookup failed)."""
    issueTitle: String
    """One of succeeded | failed | crashed."""
    status: String!
    endedAt: DateTime!
    """PR number if the agent produced one; null otherwise."""
    prNumber: Int
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
    """Top 10 ready-for-pickup issues (#2805 §1.3). Sourced from the most
    recent \`dispatcher.queue_snapshots\` row, joined with a GitHub API
    lookup for title/labels. Empty list when the queue is empty OR when
    the daemon has not yet written a snapshot."""
    queueReady: [QueueItem!]!
    """Top 10 blocked issues (#2805 §1.3). Fetched live from the GitHub
    API — \`status/blocked\` issues are not tracked in
    \`dispatcher.queue_snapshots\`. Empty list when the lookup fails or
    when nothing is blocked."""
    queueBlocked: [QueueItem!]!
    """Top 10 recently-completed agents (#2805 §1.5). Rows from
    \`dispatcher.agents\` where status IN ('succeeded','failed','crashed')
    ordered by \`ended_at\` DESC."""
    recentCompletions: [RecentCompletion!]!
    """All live-editable \`dispatcher.config\` entries (#2805 §1.6)."""
    config: [DispatcherConfigEntry!]!
    """\`dispatcher.config.value\` for \`spawn_frozen_until\` (§10), or null if not set."""
    spawnFrozenUntil: DateTime
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
  }

  extend type Mutation {
    """Issue an admin control command to the daemon. Writes a row to
    \`dispatcher.commands\` — the daemon consumes it on its next 30s tick.

    Idempotency: if an identical row (same command + issuedBy + payload) was
    written in the last 10 seconds and is still unconsumed, return that row
    and set \`created=false\` instead of inserting a duplicate.

    Destructive commands (\`stop\`, \`drain\`, \`force_kill\`) require fresh
    re-auth per §17 Risk 6. In Phase 1 this is enforced via the
    \`X-MFA-Token\` HTTP header — any non-empty value is accepted as a
    placeholder. Sub-task E or a follow-up wires the real MFA prompt.

    Admin-only; non-admins receive "not found".
    """
    dispatcherControl(
      command: DispatcherCommand!
      """Command-specific JSON payload. For \`retry\` / \`force_kill\` this must
      include \`agentId\`. For others, \`{}\` is fine."""
      payload: JSON
    ): DispatcherCommandResult!

    """Update a single \`dispatcher.config\` entry (#2805 §1.6). Writes the
    new value as JSONB, stamps \`updated_at\`, and records the admin's email
    in \`updated_by\` for audit. Like \`dispatcherControl\`, this requires a
    non-empty \`X-MFA-Token\` header — config edits can materially change
    daemon behaviour (e.g. lowering \`concurrency_cap\` mid-flight).

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
