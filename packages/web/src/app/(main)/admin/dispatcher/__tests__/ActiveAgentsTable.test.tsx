import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import type { DispatcherAgent } from '@/lib/dispatcher-queries';
import { ActiveAgentsTable } from '../ActiveAgentsTable';

/**
 * Tests for the ActiveAgentsTable's shared `(issue, priority, title)`
 * prefix and the phase tooltip (#2899). The row layout predates these
 * tests, but the prefix + tooltip are new — covering just them
 * exercises the issue's AC without re-testing unchanged behaviour.
 */

function makeAgent(overrides: Partial<DispatcherAgent> = {}): DispatcherAgent {
  return {
    id: 'aabbccdd-eeff-0011-2233-445566778899',
    kind: 'task',
    issueNumber: 2899,
    issueTitle: 'feat(web): unify dispatcher admin tables',
    priority: 'p2',
    worktreePath: '/Users/x/.claude/worktrees/agent-aabbccdd',
    phase: 'ralph',
    status: 'running',
    startedAt: '2026-04-20T15:00:00Z',
    endedAt: null,
    exitCode: null,
    prNumber: null,
    retriesUsed: 0,
    ...overrides,
  };
}

describe('ActiveAgentsTable — unified (issue, priority, title) prefix', () => {
  it('renders the issue link, priority badge, and title for each row', () => {
    const agent = makeAgent();
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    expect(screen.getByTestId('issue-link-2899')).toBeInTheDocument();
    expect(screen.getByTestId('priority-badge-p2')).toBeInTheDocument();
    expect(
      screen.getByTestId('active-agent-title').textContent,
    ).toContain('unify dispatcher admin tables');
  });

  it('renders an em-dash placeholder when priority is null', () => {
    const agent = makeAgent({ priority: null });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    // PriorityBadge renders the em-dash glyph for null priority.
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('renders a placeholder when the title is null (pre-#2820 agent)', () => {
    const agent = makeAgent({ issueTitle: null });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    expect(screen.getByText('(title unavailable)')).toBeInTheDocument();
  });
});

describe('ActiveAgentsTable — phase tooltip', () => {
  it('renders the phase chip with a native title tooltip including the flow', () => {
    const agent = makeAgent({ phase: 'ralph' });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const chip = screen.getByTestId(`active-agent-phase-${agent.id}`);
    const tooltip = chip.getAttribute('title');
    expect(tooltip).not.toBeNull();
    // Both the "Currently in X" preamble and the Flow line must be present.
    expect(tooltip).toContain('Currently in ralph phase.');
    expect(tooltip).toContain('Flow: ');
    // Full forward flow is present (happy-path phases + conditional marker).
    expect(tooltip).toContain('claiming');
    expect(tooltip).toContain('planning');
    expect(tooltip).toContain('setup');
    expect(tooltip).toContain('ralph');
    expect(tooltip).toContain('summary');
    expect(tooltip).toContain('fix_ci*');
    expect(tooltip).toContain('merge');
    expect(tooltip).toContain('verify');
    expect(tooltip).toContain('retro');
    // Current phase is bracketed so the operator can locate their position.
    expect(tooltip).toContain('[ralph]');
  });

  it('rebuilds the tooltip for each phase — different phases get different highlights', () => {
    const agent = makeAgent({ phase: 'summary' });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const chip = screen.getByTestId(`active-agent-phase-${agent.id}`);
    expect(chip.getAttribute('title')).toContain('[summary]');
    expect(chip.getAttribute('title')).toContain('Currently in summary phase.');
  });
});

describe('ActiveAgentsTable — empty state', () => {
  it('renders an empty message when no agents are active', () => {
    render(<ActiveAgentsTable agents={[]} onAgentAction={vi.fn()} />);
    expect(screen.getByText('No active agents.')).toBeInTheDocument();
  });
});
