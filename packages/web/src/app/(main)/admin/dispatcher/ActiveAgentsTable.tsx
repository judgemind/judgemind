'use client';

import { Button } from '@/components/ui/button';
import { SECTION_HEADING } from '@/lib/typography';
import type { DispatcherAgent } from '@/lib/dispatcher-queries';
import { formatUptime, shortAgentId, worktreeLogsUrl } from './format-helpers';
import { IssueLink } from './ui-primitives';

interface ActiveAgentsTableProps {
  agents: readonly DispatcherAgent[];
  disabled?: boolean;
  onAgentAction: (command: 'retry' | 'force_kill', agentId: string) => void;
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
  onAction: (command: 'retry' | 'force_kill', agentId: string) => void;
  nowMs?: number;
}) {
  const logsHref = worktreeLogsUrl(agent.worktreePath);
  const elapsed = formatUptime(agent.startedAt, nowMs);
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm hover:bg-muted/50">
      <span
        className="w-20 flex-shrink-0 font-mono text-xs text-foreground"
        title={agent.id}
      >
        {shortAgentId(agent.id)}
      </span>
      <span className="flex-shrink-0">
        <IssueLink number={agent.issueNumber} />
      </span>
      <span className="min-w-0 flex-1 font-mono text-xs text-muted-foreground">
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
          onClick={() => onAction('force_kill', agent.id)}
        >
          Kill
        </Button>
      </span>
    </li>
  );
}
