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

describe('ActiveAgentsTable — Magic Move view-transition names (#2967)', () => {
  it('renders outer <li> with view-transition-name: issue-<N>', () => {
    const agent = makeAgent({ id: 'agent-42', issueNumber: 2967 });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const outer = screen.getByTestId(`active-agent-row-${agent.id}`);
    // React camel-cases `viewTransitionName`; jsdom exposes it on the
    // `style` property. Assert the exact value so a future rename of
    // the prefix fails loudly.
    expect((outer as HTMLElement).style.viewTransitionName).toBe(
      `issue-${agent.issueNumber}`,
    );
  });

  it('renders inner wrapper with view-transition-name: agent-<id>', () => {
    const agent = makeAgent({ id: 'agent-42', issueNumber: 2967 });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const inner = screen.getByTestId(`active-agent-inner-${agent.id}`);
    expect((inner as HTMLElement).style.viewTransitionName).toBe(
      `agent-${agent.id}`,
    );
  });

  it('nests the agent wrapper inside the issue wrapper (enables the failure-retry split)', () => {
    const agent = makeAgent({ id: 'agent-42', issueNumber: 2967 });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const outer = screen.getByTestId(`active-agent-row-${agent.id}`);
    const inner = screen.getByTestId(`active-agent-inner-${agent.id}`);
    // The inner `agent-X` wrapper must be a DOM descendant of the outer
    // `issue-N` <li>. That nesting is what lets the same frame animate
    // the inner card into Completed while the outer shell flies back
    // to the Queue on a failure-with-retry (see tmp/magic-move-proto).
    expect(outer.contains(inner)).toBe(true);
  });
});

describe('ActiveAgentsTable — final-attempt Opus marker (#3021)', () => {
  it('renders (opus) in chip text and Final attempt in tooltip when retriesUsed=2 and phase=ralph', () => {
    const agent = makeAgent({ phase: 'ralph', retriesUsed: 2 });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const chip = screen.getByTestId(`active-agent-phase-${agent.id}`);
    expect(chip.textContent).toContain('(opus)');
    expect(chip.getAttribute('title')).toContain('Final attempt');
  });

  it('does NOT render (opus) when retriesUsed=0 and phase=ralph', () => {
    const agent = makeAgent({ phase: 'ralph', retriesUsed: 0 });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const chip = screen.getByTestId(`active-agent-phase-${agent.id}`);
    expect(chip.textContent).toBe('ralph');
    expect(chip.getAttribute('title')).not.toContain('Final attempt');
  });

  it('does NOT render (opus) on a non-ralph phase even when retriesUsed=2', () => {
    const agent = makeAgent({ phase: 'planning', retriesUsed: 2 });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const chip = screen.getByTestId(`active-agent-phase-${agent.id}`);
    expect(chip.textContent).toBe('planning');
  });
});

describe('ActiveAgentsTable — #3206 view-transition gate while dialog open', () => {
  it('drops view-transition-name on BOTH wrappers when dialogOpen=true', () => {
    const agent = makeAgent({ id: 'agent-42', issueNumber: 3206 });
    render(
      <ActiveAgentsTable
        agents={[agent]}
        onAgentAction={vi.fn()}
        dialogOpen
      />,
    );
    const outer = screen.getByTestId(`active-agent-row-${agent.id}`);
    const inner = screen.getByTestId(`active-agent-inner-${agent.id}`);
    // Both wrappers must drop the name — leaving either in place would
    // still let view-transition pseudo-elements paint over the dialog.
    expect((outer as HTMLElement).style.viewTransitionName).toBe('');
    expect((inner as HTMLElement).style.viewTransitionName).toBe('');
  });

  it('keeps view-transition-name when dialogOpen=false (default — preserves Magic Move)', () => {
    const agent = makeAgent({ id: 'agent-42', issueNumber: 3206 });
    render(
      <ActiveAgentsTable agents={[agent]} onAgentAction={vi.fn()} />,
    );
    const outer = screen.getByTestId(`active-agent-row-${agent.id}`);
    const inner = screen.getByTestId(`active-agent-inner-${agent.id}`);
    expect((outer as HTMLElement).style.viewTransitionName).toBe(
      `issue-${agent.issueNumber}`,
    );
    expect((inner as HTMLElement).style.viewTransitionName).toBe(
      `agent-${agent.id}`,
    );
  });
});
