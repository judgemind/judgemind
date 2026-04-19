'use client';

import { Button } from '@/components/ui/button';
import { SECTION_HEADING } from '@/lib/typography';
import type { DispatcherAgent } from '@/lib/dispatcher-queries';
import { formatUptime, shortAgentId, worktreeLogsUrl } from './format-helpers';

interface ActiveAgentsTableProps {
  agents: readonly DispatcherAgent[];
  disabled?: boolean;
  onAgentAction: (command: 'retry' | 'force_kill', agentId: string) => void;
  /** Override for deterministic tests. */
  nowMs?: number;
}

export function ActiveAgentsTable({
  agents,
  disabled,
  onAgentAction,
  nowMs,
}: ActiveAgentsTableProps) {
  return (
    <section
      aria-labelledby="active-agents-heading"
      className="rounded-lg border border-border bg-card"
    >
      <div className="flex items-center justify-between border-b border-border p-4">
        <h2 id="active-agents-heading" className={SECTION_HEADING}>
          Active agents ({agents.length})
        </h2>
      </div>

      {agents.length === 0 ? (
        <p className="p-6 text-center text-sm text-muted-foreground">No active agents.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th scope="col" className="p-3">Agent</th>
                <th scope="col" className="p-3">Issue</th>
                <th scope="col" className="p-3">Phase</th>
                <th scope="col" className="p-3">Elapsed</th>
                <th scope="col" className="p-3">Worktree</th>
                <th scope="col" className="p-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {agents.map((agent) => {
                const logsHref = worktreeLogsUrl(agent.worktreePath);
                return (
                  <tr
                    key={agent.id}
                    className="border-b border-border last:border-b-0"
                  >
                    <td className="p-3 font-mono text-xs text-foreground">
                      {shortAgentId(agent.id)}
                    </td>
                    <td className="p-3 text-foreground">#{agent.issueNumber}</td>
                    <td className="p-3 font-mono text-xs text-muted-foreground">
                      {agent.phase}
                    </td>
                    <td className="p-3 text-foreground">
                      {formatUptime(agent.startedAt, nowMs)}
                    </td>
                    <td className="p-3 text-xs">
                      {logsHref ? (
                        <a
                          href={logsHref}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-accent underline-offset-2 hover:underline"
                        >
                          Logs
                        </a>
                      ) : (
                        <span
                          title={agent.worktreePath}
                          className="font-mono text-muted-foreground"
                        >
                          {agent.worktreePath.split('/').pop() ?? agent.worktreePath}
                        </span>
                      )}
                    </td>
                    <td className="p-3">
                      <div className="flex gap-2">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={disabled}
                          onClick={() => onAgentAction('retry', agent.id)}
                        >
                          Retry
                        </Button>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          disabled={disabled}
                          onClick={() => onAgentAction('force_kill', agent.id)}
                        >
                          Kill
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
