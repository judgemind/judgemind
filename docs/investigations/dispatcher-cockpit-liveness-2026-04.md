# Dispatcher Cockpit Liveness — Investigation (2026-04-23)

## Context

The dispatcher admin cockpit (`/admin/dispatcher`) was reported to "disconnect" from the backend somewhat regularly. This note maps the full update path end-to-end, identifies what "disconnect" actually means in this architecture, and lists the likely root causes.

Filed follow-up: **#3141** — `fix(web): dispatcher cockpit liveness — tighten heartbeat, add poll backoff, soft-reconnect on 401`. When #3141 ships, it is expected to resolve the user-visible "disconnect" symptoms described here. Re-verify after merge; if the cockpit still feels janky, reopen the SSE / LISTEN-NOTIFY decision discussed at the end of this note.

## Update path, end-to-end

All state flows through Postgres. There is no Redis, no SSE, no WebSocket, and no push anywhere in the stack — the page does not hold any persistent connection.

### 1. Daemon → Postgres

- Scheduler tick: **30s** — writes `dispatcher.queue_snapshots` and agent state. `scripts/dispatcher/daemon.py:119-120`, `daemon.py:3206-3222`.
- Supervisor tick: **120s** — UPDATEs `dispatcher.runs.heartbeat_ts`. `daemon.py:2174`.
- Blocked-list scan: slower cadence → `dispatcher.blocked_snapshots`. `daemon.py:3360`.

### 2. Web API → DB

- `packages/api/src/graphql/dispatcher/resolvers.ts` — direct `SELECT` per GraphQL request against `dispatcher.*` tables.
- No cache layer, no subscriptions. Spec explicitly forbids subscriptions: `docs/specs/dispatcher-v2-spec.md:360` ("No subscriptions").

### 3. Browser → Web API

- `packages/web/src/app/(main)/admin/dispatcher/DispatcherDashboard.tsx:32,113-120` — Apollo `useQuery` with `pollInterval: 2000`, `fetchPolicy: 'cache-and-network'`.
- 2-second HTTP polling. Any single poll failure flips the stale yellow indicator immediately (`DispatcherDashboard.tsx:302-309`).
- Hard-fail banner only when there is no cached data yet (first-load failure).

### 4. Heartbeat freshness pill

- `packages/web/src/app/(main)/admin/dispatcher/DispatcherHeader.tsx:46` — pill turns red "unhealthy" when `heartbeat_ts` is older than 3 minutes.

## What "disconnects regularly" actually is

The page never holds an open connection, so there is nothing to disconnect. "Disconnected" maps to one of three UI states:

1. The yellow stale indicator (any failed poll).
2. The red "unhealthy" heartbeat pill (heartbeat > 180s old).
3. The hard-fail error banner (first poll failed with no cached data).

## Likely root causes, ranked

1. **Daemon redeploy → red pill for 2–4 min.** Supervisor tick is 120s and the unhealthy threshold is 180s, so a normal Fargate restart reliably flips the pill before the new daemon writes its first heartbeat.
2. **Auth token refresh edge cases.** `packages/web/src/lib/apollo-client.ts:64-127`. Mid-session expiry rejects queued polls; if refresh fails there is no in-app recovery path.
3. **No backoff on failed polls.** A slow backend is hammered every 2s, polls stack, each takes longer, and the stale indicator flickers.
4. **DB-query latency spikes.** Snapshot tables grow; contention makes all 2s polls block together.
5. **Web-service redeploy.** Page stays loaded, GraphQL endpoint unreachable for the redeploy window → stale indicator for 30–60s.

## Cheap wins (tracked in #3141)

- Write heartbeat on every scheduler tick (~30s) instead of only the supervisor tick.
- Drop the unhealthy threshold from 180s to 90s.
- Exponential backoff with jitter on poll failures (2s → 4s → 8s → 16s → 30s cap, reset on success).
- Debounce the stale indicator — do not flip on a single blip.
- Silent token-refresh retry on 401 instead of freezing the dashboard.

## The bigger change (not yet filed)

Postgres `LISTEN/NOTIFY` + SSE push on a `/admin/dispatcher/stream` endpoint. Daemon `NOTIFY`s after each tick; web API fans out via `EventSource`; polling goes away.

### Pros

- Latency drops from ~1–2s to tens of ms. Feels live.
- Zero DB load when the daemon is idle.
- "Stale" becomes meaningful — real connection drop instead of "last poll failed."
- `EventSource` reconnect semantics make daemon redeploys transparent.
- Scales better as more panels/drawers are added.

### Cons

- Spec change. `docs/specs/dispatcher-v2-spec.md:360` explicitly forbids subscriptions — needs a `type/decision` issue.
- Stateful web tier. SSE on Vercel needs a streaming-capable route (Node runtime or edge with streaming); non-trivial infra delta.
- Postgres `LISTEN` holds a long-lived DB connection on the web API; needs reconnect handling.
- Auth over SSE is fiddlier — `EventSource` has no headers, so tokens go via query string or cookies; the Apollo auth/refresh flow does not translate.
- Migration carries two code paths during rollout.
- Testability regression — polling is trivial to test; SSE + NOTIFY needs real-Postgres integration tests.
- May not be the bottleneck. After #3141 lands, the residual floor is the 30s scheduler cadence. SSE only pays off if daemon snapshot writes also tighten.

### Recommendation

Ship #3141 first and measure. File the SSE proposal as `type/decision` only if the cockpit still feels janky after the cheap wins land.
