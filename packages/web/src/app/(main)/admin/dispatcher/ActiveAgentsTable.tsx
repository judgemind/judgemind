'use client';

import type { CSSProperties } from 'react';
import { Button } from '@/components/ui/button';
import { SECTION_HEADING } from '@/lib/typography';
import type { DispatcherAgent } from '@/lib/dispatcher-queries';
import { tooltipText } from '@/lib/dispatcher-phase-flow';
import { formatUptime, shortAgentId, worktreeLogsUrl } from './format-helpers';
import { IssueLink, PriorityBadge } from './ui-primitives';

interface ActiveAgentsTableProps {
  agents: readonly DispatcherAgent[];
  disabled?: boolean;
  onAgentAction: (command: 'retry' | 'force_stop', agentId: string) => void;
  /** Override for deterministic tests. */
  nowMs?: number;
}

/**
 * Dense per-agent row layout (#2805 §1.4). Replaces the prior thick-bordered
 * `<table>` card with a borderless section of `<ul>` rows that match the
 * queue/completed visual rhythm. The issue number is a hot link via
 * `IssueLink`; every action button uses the new `size="xs"` variant.
 */
export function ActiveAgentsTable({
  agents,
  disabled,
  onAgentAction,
  nowMs,
}: ActiveAgentsTableProps) {
  return (
    <section aria-labelledby="active-agents-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="active-agents-heading" className={SECTION_HEADING}>
          Active agents ({agents.length})
        </h2>
      </div>
      {agents.length === 0 ? (
        <p className="py-2 text-sm text-muted-foreground">No active agents.</p>
      ) : (
        <ul className="divide-y divide-border">
          {agents.map((agent) => (
            <ActiveAgentRow
              key={agent.id}
              agent={agent}
              disabled={disabled}
              onAction={onAgentAction}
              nowMs={nowMs}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function ActiveAgentRow({
  agent,
  disabled,
  onAction,
  nowMs,
}: {
  agent: DispatcherAgent;
  disabled?: boolean;
  onAction: (command: 'retry' | 'force_stop', agentId: string) => void;
  nowMs?: number;
}) {
  const logsHref = worktreeLogsUrl(agent.worktreePath);
  const elapsed = formatUptime(agent.startedAt, nowMs);
  // Build the phase tooltip from the shared canonical constant so
  // future phase additions propagate automatically (#2899).
  const phaseTooltip = tooltipText(agent.phase);
  // Magic Move wiring (#2967). The outer <li> carries the issue identity
  // (`issue-N`) so the row Magic Moves between Queue and Active when the
  // agent is claimed, retried, or released. The inner wrapper carries
  // the agent identity (`agent-X`) so the same agent's card Magic Moves
  // from Active to Recently Completed on terminal. The nested structure
  // is what enables the failure-with-retry split: the inner (agent)
  // animates completed-ward while the outer (issue) animates queue-
  // ward in the same frame. See tmp/magic-move-proto.html for the
  // reference visual. CSSProperties doesn't yet type-check
  // `viewTransitionName`; we spread the key in via a narrow cast.
  const outerStyle = {
    viewTransitionName: `issue-${agent.issueNumber}`,
  } as CSSProperties;
  const innerStyle = {
    viewTransitionName: `agent-${agent.id}`,
  } as CSSProperties;
  return (
    <li
      className="py-2 text-sm hover:bg-muted/50"
      style={outerStyle}
      data-testid={`active-agent-row-${agent.id}`}
    >
      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-1"
        style={innerStyle}
        data-testid={`active-agent-inner-${agent.id}`}
      >
        <span
          className="w-20 flex-shrink-0 font-mono text-xs text-foreground"
          title={agent.id}
        >
          {shortAgentId(agent.id)}
        </span>
        {/* Unified (issue, priority, title) prefix — matches QueuePanel
            and RecentCompletionsPanel (#2899). */}
        <span className="flex-shrink-0">
          <IssueLink number={agent.issueNumber} />
        </span>
        <span className="flex-shrink-0">
          <PriorityBadge priority={agent.priority} />
        </span>
        <span
          className="min-w-0 flex-1 truncate text-foreground"
          data-testid="active-agent-title"
          title={agent.issueTitle ?? undefined}
        >
          {agent.issueTitle ?? (
            <span className="italic text-muted-foreground">
              (title unavailable)
            </span>
          )}
        </span>
        <span
          className="flex-shrink-0 cursor-help rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
          data-testid={`active-agent-phase-${agent.id}`}
          title={phaseTooltip}
        >
          {agent.phase}
        </span>
        <span className="w-16 flex-shrink-0 text-right font-mono text-xs text-muted-foreground">
          {elapsed}
        </span>
        <span className="flex-shrink-0 text-xs">
          {logsHref ? (
            <a
              href={logsHref}
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand-accent dark:text-brand-accent-light underline-offset-2 hover:underline"
            >
              Logs &rarr;
            </a>
          ) : (
            <span
              title={agent.worktreePath}
              className="font-mono text-muted-foreground"
            >
              {agent.worktreePath.split('/').pop() ?? agent.worktreePath}
            </span>
          )}
        </span>
        <span className="flex flex-shrink-0 gap-1">
          <Button
            type="button"
            variant="outline"
            size="xs"
            disabled={disabled}
            onClick={() => onAction('retry', agent.id)}
          >
            Retry
          </Button>
          <Button
            type="button"
            variant="destructive"
            size="xs"
            disabled={disabled}
            onClick={() => onAction('force_stop', agent.id)}
          >
            Kill
          </Button>
        </span>
      </div>
    </li>
  );
}
