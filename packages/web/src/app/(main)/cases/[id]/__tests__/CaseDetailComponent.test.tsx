import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MockedProvider, MockedResponse } from '@apollo/client/testing';
import { gql } from '@apollo/client';
import { CaseDetail } from '../CaseDetail';

// ---------------------------------------------------------------------------
// Mock next/link — render as an anchor tag
// ---------------------------------------------------------------------------

vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

// ---------------------------------------------------------------------------
// GraphQL queries (must match the component's queries exactly)
// ---------------------------------------------------------------------------

const CASE_QUERY = gql`
  query CaseDetail($id: ID!) {
    case(id: $id) {
      id
      caseNumber
      caseTitle
      caseType
      caseStatus
      filedAt
      court {
        courtName
        county
      }
      parties {
        id
        canonicalName
        partyType
        role
      }
    }
  }
`;

const CASE_RULINGS_QUERY = gql`
  query CaseRulings($caseId: ID!, $first: Int!, $after: String) {
    rulings(caseId: $caseId, first: $first, after: $after) {
      edges {
        cursor
        node {
          id
          hearingDate
          motionType
          outcome
          isTentative
          department
          judge {
            canonicalName
          }
          rulingText
          rulingTextHtml
          documentId
          documentFormat
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
`;

// ---------------------------------------------------------------------------
// Test data factories
// ---------------------------------------------------------------------------

function buildCaseData() {
  return {
    case: {
      id: 'case-1',
      caseNumber: '25STCV12345',
      caseTitle: 'Smith v. Jones',
      caseType: 'civil',
      caseStatus: 'open',
      filedAt: '2025-01-15' as string | null,
      court: {
        courtName: 'Los Angeles Superior Court',
        county: 'Los Angeles',
      },
      parties: [
        { id: 'p1', canonicalName: 'Smith', partyType: null, role: 'plaintiff' },
        { id: 'p2', canonicalName: 'Jones', partyType: null, role: 'defendant' },
      ],
    },
  };
}

function buildRulingNode(overrides: Partial<{
  id: string;
  hearingDate: string;
  motionType: string | null;
  outcome: string | null;
  isTentative: boolean;
  department: string | null;
  judge: { canonicalName: string } | null;
  rulingText: string | null;
  rulingTextHtml: string | null;
  documentId: string | null;
  documentFormat: string | null;
}> = {}) {
  return {
    id: 'ruling-1',
    hearingDate: '2026-03-10',
    motionType: 'msj',
    outcome: 'granted',
    isTentative: true,
    department: '12',
    judge: { canonicalName: 'Johnson, Robert M.' },
    rulingText: 'The motion is granted.',
    rulingTextHtml: null,
    documentId: null,
    documentFormat: null,
    ...overrides,
  };
}

function buildRulingsData(nodes: ReturnType<typeof buildRulingNode>[]) {
  return {
    rulings: {
      edges: nodes.map((node, i) => ({ cursor: `cursor-${i}`, node })),
      pageInfo: { hasNextPage: false, endCursor: null },
    },
  };
}

function buildMocks(
  caseData: ReturnType<typeof buildCaseData>,
  rulingsData: ReturnType<typeof buildRulingsData>,
): MockedResponse[] {
  return [
    {
      request: {
        query: CASE_QUERY,
        variables: { id: 'case-1' },
      },
      result: { data: caseData },
    },
    {
      request: {
        query: CASE_RULINGS_QUERY,
        variables: { caseId: 'case-1', first: 20 },
      },
      result: { data: rulingsData },
    },
  ];
}

function renderWithProvider(mocks: MockedResponse[]) {
  return render(
    <MockedProvider mocks={mocks}>
      <CaseDetail caseId="case-1" />
    </MockedProvider>,
  );
}

/** Helper: expand a ruling card by clicking its header button. */
async function expandRuling(label: RegExp) {
  const btn = await screen.findByLabelText(label, {}, { timeout: 3000 });
  fireEvent.click(btn);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CaseDetail — sidebar layout (no Case Details or Judges cards)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does NOT render a "Case Details" card', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    renderWithProvider(mocks);

    // Wait for data to load — parties render twice (mobile + desktop)
    await screen.findAllByText('Plaintiffs', {}, { timeout: 3000 });

    // The "Case Details" heading should NOT be present
    expect(screen.queryByText('Case Details')).not.toBeInTheDocument();
  });

  it('does NOT render a "Judges" card', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    renderWithProvider(mocks);

    await screen.findAllByText('Plaintiffs', {}, { timeout: 3000 });

    // The "Judges" card heading should NOT be present
    expect(screen.queryByRole('heading', { name: 'Judges' })).not.toBeInTheDocument();
  });

  it('renders parties in a sidebar (not in a Card)', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    // Parties render twice (mobile + desktop sidebar)
    await screen.findAllByText('Plaintiffs', {}, { timeout: 3000 });
    const smithInstances = screen.getAllByText('Smith');
    expect(smithInstances.length).toBeGreaterThanOrEqual(1);
    const defLabels = screen.getAllByText('Defendants');
    expect(defLabels.length).toBeGreaterThanOrEqual(1);
    const jonesInstances = screen.getAllByText('Jones');
    expect(jonesInstances.length).toBeGreaterThanOrEqual(1);

    // Parties sidebar should exist (data-testid)
    expect(container.querySelector('[data-testid="parties-sidebar"]')).toBeInTheDocument();
  });

  it('renders filed date in the sidebar', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    renderWithProvider(mocks);

    // Filed Date renders twice (mobile + desktop)
    const filedLabels = await screen.findAllByText('Filed Date', {}, { timeout: 3000 });
    expect(filedLabels.length).toBeGreaterThanOrEqual(1);
  });

  it('uses a 2-column grid layout on desktop', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    await screen.findAllByText('Plaintiffs', {}, { timeout: 3000 });

    // The root layout element should have the responsive grid classes
    const gridEl = container.querySelector('.grid');
    expect(gridEl).toBeInTheDocument();
    expect(gridEl?.classList.contains('sm:grid-cols-[1fr_280px]')).toBe(true);
  });

  it('renders an aside element for desktop sidebar', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    await screen.findAllByText('Plaintiffs', {}, { timeout: 3000 });

    const aside = container.querySelector('aside');
    expect(aside).toBeInTheDocument();
    expect(aside?.getAttribute('aria-label')).toBe('Case parties');
  });

  it('renders rulings section', async () => {
    const node = buildRulingNode();
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    renderWithProvider(mocks);

    const heading = await screen.findByText('Rulings', {}, { timeout: 3000 });
    expect(heading).toBeInTheDocument();
  });

  it('renders rulings as Cards with expand/collapse', async () => {
    const node = buildRulingNode();
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    renderWithProvider(mocks);

    // Ruling header should be visible
    const rulingBtn = await screen.findByLabelText(/Ruling from/, {}, { timeout: 3000 });
    expect(rulingBtn).toBeInTheDocument();
    expect(rulingBtn.getAttribute('aria-expanded')).toBe('false');

    // Ruling text should NOT be visible before expanding
    expect(screen.queryByText('The motion is granted.')).not.toBeInTheDocument();

    // Click to expand
    fireEvent.click(rulingBtn);
    expect(rulingBtn.getAttribute('aria-expanded')).toBe('true');

    // Now ruling text should be visible
    expect(screen.getByText('The motion is granted.')).toBeInTheDocument();
  });

  it('uses Badge components for outcome and tentative status', async () => {
    const node = buildRulingNode();
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    await screen.findByText('Granted', {}, { timeout: 3000 });

    // Badge uses rounded-full class
    expect(container.querySelectorAll('.rounded-full').length).toBeGreaterThan(0);
    expect(screen.getByText('Tentative')).toBeInTheDocument();
  });

  it('does not render sidebar when no parties and no filedAt', async () => {
    const caseData = buildCaseData();
    caseData.case.parties = [];
    caseData.case.filedAt = null;
    const mocks = buildMocks(caseData, buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    // Wait for rulings heading to confirm component is loaded
    await screen.findByText('Rulings', {}, { timeout: 3000 });

    // Sidebar should not exist since there are no parties or filed date
    expect(container.querySelector('[data-testid="parties-sidebar"]')).not.toBeInTheDocument();
  });
});

describe('CaseDetail — ruling HTML rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders formatted HTML when rulingTextHtml is present', async () => {
    const node = buildRulingNode({
      rulingTextHtml: '<div class="ruling"><p>The motion is <strong>GRANTED</strong>.</p></div>',
      rulingText: 'The motion is GRANTED.',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Expand the ruling card first
    await expandRuling(/Ruling from/);

    // Should render sanitized HTML via dangerouslySetInnerHTML
    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('<strong>GRANTED</strong>');
  });

  it('falls back to plain text when rulingTextHtml is null', async () => {
    const node = buildRulingNode({
      rulingTextHtml: null,
      rulingText: 'The motion is granted.\n\nPlaintiff prevails.',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Expand the ruling card first
    await expandRuling(/Ruling from/);

    // Should render as plain text paragraphs, not via dangerouslySetInnerHTML
    expect(container.querySelector('.ruling-content')).not.toBeInTheDocument();
    expect(screen.getByText('The motion is granted.')).toBeInTheDocument();
    expect(screen.getByText('Plaintiff prevails.')).toBeInTheDocument();
  });

  it('sanitizes HTML — strips script tags', async () => {
    const node = buildRulingNode({
      rulingTextHtml: '<p>Safe content</p><script>alert("xss")</script>',
      rulingText: 'Safe content',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Expand the ruling card first
    await expandRuling(/Ruling from/);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('Safe content');
    expect(rulingContent?.innerHTML).not.toContain('script');
    expect(rulingContent?.innerHTML).not.toContain('alert');
  });

  it('sanitizes HTML — strips event handlers', async () => {
    const node = buildRulingNode({
      rulingTextHtml: '<p onclick="alert(1)" onerror="alert(2)">Content</p>',
      rulingText: 'Content',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Expand the ruling card first
    await expandRuling(/Ruling from/);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).not.toContain('onclick');
    expect(rulingContent?.innerHTML).not.toContain('onerror');
  });

  it('shows full text when ruling card is expanded (card-based collapse)', async () => {
    const longText = 'A'.repeat(600);
    const node = buildRulingNode({
      rulingTextHtml: `<p>${longText}</p>`,
      rulingText: longText,
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Expand the ruling card
    await expandRuling(/Ruling from/);

    // Full content should be visible — no CSS truncation in the new Card layout
    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain(longText);

    // No gradient overlay or max-height truncation
    expect(container.querySelector('.bg-gradient-to-t')).not.toBeInTheDocument();
    expect(rulingContent?.classList.contains('max-h-40')).not.toBe(true);
  });

  it('renders only rulingTextHtml when rulingText is null', async () => {
    const node = buildRulingNode({
      rulingTextHtml: '<p>Only HTML available</p>',
      rulingText: null,
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Expand the ruling card
    await expandRuling(/Ruling from/);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('Only HTML available');
  });

  it('does not render ruling text section when both are null', async () => {
    const node = buildRulingNode({
      rulingTextHtml: null,
      rulingText: null,
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Wait for case data to load — parties render twice (mobile + desktop)
    await screen.findAllByText('Plaintiffs', {}, { timeout: 3000 });

    // No ruling content should be rendered
    expect(container.querySelector('.ruling-content')).not.toBeInTheDocument();
  });
});
