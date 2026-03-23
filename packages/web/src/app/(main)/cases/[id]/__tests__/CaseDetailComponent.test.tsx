import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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
      judges {
        id
        canonicalName
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
      judges: [
        { id: 'j1', canonicalName: 'Johnson, Robert M.' },
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
// Tests — parties header layout
// ---------------------------------------------------------------------------

describe('CaseDetail — parties header layout', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does NOT render a "Case Details" card', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    renderWithProvider(mocks);

    await screen.findByText('Plaintiffs', {}, { timeout: 3000 });

    // The "Case Details" heading should NOT be present
    expect(screen.queryByText('Case Details')).not.toBeInTheDocument();
  });

  it('does NOT render a "Judges" card', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    renderWithProvider(mocks);

    await screen.findByText('Plaintiffs', {}, { timeout: 3000 });

    // The "Judges" card heading should NOT be present
    expect(screen.queryByRole('heading', { name: 'Judges' })).not.toBeInTheDocument();
  });

  it('renders parties in a header area (not a sidebar)', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    await screen.findByText('Plaintiffs', {}, { timeout: 3000 });
    expect(screen.getByText('Smith')).toBeInTheDocument();
    expect(screen.getByText('Defendants')).toBeInTheDocument();
    expect(screen.getByText('Jones')).toBeInTheDocument();

    // Parties header should exist (data-testid)
    expect(container.querySelector('[data-testid="parties-header"]')).toBeInTheDocument();
    // Old sidebar should NOT exist
    expect(container.querySelector('[data-testid="parties-sidebar"]')).not.toBeInTheDocument();
  });

  it('does not use a 2-column grid layout (no sidebar)', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    await screen.findByText('Plaintiffs', {}, { timeout: 3000 });

    // No sidebar grid — should NOT have sm:grid-cols-[1fr_280px]
    const gridEl = container.querySelector('.sm\\:grid-cols-\\[1fr_280px\\]');
    expect(gridEl).not.toBeInTheDocument();
  });

  it('does not render an aside element', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    await screen.findByText('Plaintiffs', {}, { timeout: 3000 });

    const aside = container.querySelector('aside');
    expect(aside).not.toBeInTheDocument();
  });

  it('renders filed date in the header area', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    renderWithProvider(mocks);

    const filedLabel = await screen.findByText('Filed Date', {}, { timeout: 3000 });
    expect(filedLabel).toBeInTheDocument();
  });

  it('does not render parties header when no parties and no filedAt', async () => {
    const caseData = buildCaseData();
    caseData.case.parties = [];
    caseData.case.filedAt = null;
    const mocks = buildMocks(caseData, buildRulingsData([]));
    const { container } = renderWithProvider(mocks);

    // Wait for rulings heading to confirm component is loaded
    await screen.findByText('Rulings', {}, { timeout: 3000 });

    // Parties header should not exist since there are no parties or filed date
    expect(container.querySelector('[data-testid="parties-header"]')).not.toBeInTheDocument();
  });

  it('renders parties only once (no mobile/desktop duplication)', async () => {
    const mocks = buildMocks(buildCaseData(), buildRulingsData([]));
    renderWithProvider(mocks);

    await screen.findByText('Plaintiffs', {}, { timeout: 3000 });

    // Parties should render exactly once (no sidebar duplication)
    const smithInstances = screen.getAllByText('Smith');
    expect(smithInstances).toHaveLength(1);
    const jonesInstances = screen.getAllByText('Jones');
    expect(jonesInstances).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Tests — ruling rows: judge and department deduplication
// ---------------------------------------------------------------------------

describe('CaseDetail — ruling row judge/department deduplication', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not show department on ruling rows', async () => {
    const node = buildRulingNode({ department: '32' });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    renderWithProvider(mocks);

    await screen.findByLabelText(/Ruling from/, {}, { timeout: 3000 });

    // Department should NOT appear in the ruling row
    expect(screen.queryByText('Dept. 32')).not.toBeInTheDocument();
  });

  it('does not show judge name when it matches case-level judge', async () => {
    // Default ruling has judge 'Johnson, Robert M.' which matches the case judge
    const node = buildRulingNode();
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    await screen.findByLabelText(/Ruling from/, {}, { timeout: 3000 });

    // The judge name should NOT be shown on the ruling row
    expect(container.querySelector('[data-testid="ruling-judge"]')).not.toBeInTheDocument();
  });

  it('shows judge name when it differs from case-level judge (substitute judge)', async () => {
    const node = buildRulingNode({
      judge: { canonicalName: 'Williams, Sarah K.' },
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    await screen.findByLabelText(/Ruling from/, {}, { timeout: 3000 });

    // The substitute judge name SHOULD be shown
    const judgeEl = container.querySelector('[data-testid="ruling-judge"]');
    expect(judgeEl).toBeInTheDocument();
    expect(judgeEl?.textContent).toBe('Williams, Sarah K.');
  });

  it('does not show judge name when ruling has no judge', async () => {
    const node = buildRulingNode({ judge: null });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    await screen.findByLabelText(/Ruling from/, {}, { timeout: 3000 });

    expect(container.querySelector('[data-testid="ruling-judge"]')).not.toBeInTheDocument();
  });

  it('shows judge when case has no judges (empty judges array)', async () => {
    const caseData = buildCaseData();
    caseData.case.judges = [];
    const node = buildRulingNode({
      judge: { canonicalName: 'Johnson, Robert M.' },
    });
    const mocks = buildMocks(caseData, buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    await screen.findByLabelText(/Ruling from/, {}, { timeout: 3000 });

    // When case has no judges assigned, ruling judge should always show
    const judgeEl = container.querySelector('[data-testid="ruling-judge"]');
    expect(judgeEl).toBeInTheDocument();
    expect(judgeEl?.textContent).toBe('Johnson, Robert M.');
  });

  it('hides judge when ruling judge matches any of multiple case judges', async () => {
    const caseData = buildCaseData();
    caseData.case.judges = [
      { id: 'j1', canonicalName: 'Johnson, Robert M.' },
      { id: 'j2', canonicalName: 'Williams, Sarah K.' },
    ];
    const node = buildRulingNode({
      judge: { canonicalName: 'Williams, Sarah K.' },
    });
    const mocks = buildMocks(caseData, buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    await screen.findByLabelText(/Ruling from/, {}, { timeout: 3000 });

    // Williams matches one of the case judges, so should NOT show
    expect(container.querySelector('[data-testid="ruling-judge"]')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests — rulings rendering (existing)
// ---------------------------------------------------------------------------

describe('CaseDetail — rulings section', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
});

// ---------------------------------------------------------------------------
// Tests — ruling HTML rendering
// ---------------------------------------------------------------------------

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

    // Wait for case data to load
    await screen.findByText('Plaintiffs', {}, { timeout: 3000 });

    // No ruling content should be rendered
    expect(container.querySelector('.ruling-content')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests — View original document iframe (#1704)
// ---------------------------------------------------------------------------

describe('CaseDetail — View original document iframe', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows "View original" button for HTML-format documents when ruling is expanded', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // View original button should be visible
    expect(screen.getByTestId('view-original-button-ruling-1')).toBeInTheDocument();
    expect(screen.getByTestId('view-original-button-ruling-1')).toHaveTextContent('View original');
  });

  it('does not show "View original" button for PDF-format documents', async () => {
    const node = buildRulingNode({
      documentId: 'doc-pdf-1',
      documentFormat: 'pdf',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // View original button should NOT be visible for PDF
    expect(screen.queryByTestId('view-original-button-ruling-1')).not.toBeInTheDocument();
    // But download link should still be visible
    expect(screen.getByText('Download original')).toBeInTheDocument();
  });

  it('does not show "View original" button when documentId is null', async () => {
    const node = buildRulingNode({
      documentId: null,
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // Neither button should be visible
    expect(screen.queryByTestId('view-original-button-ruling-1')).not.toBeInTheDocument();
  });

  it('keeps download link alongside "View original" for HTML documents', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // Both "View original" and "Download original" should be present
    expect(screen.getByTestId('view-original-button-ruling-1')).toBeInTheDocument();
    expect(screen.getByText('Download original')).toBeInTheDocument();
  });

  it('loads and displays iframe on "View original" click', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const htmlContent = '<html><body><p>Court ruling content</p></body></html>';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(htmlContent, {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      }),
    );

    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // Click "View original"
    fireEvent.click(screen.getByTestId('view-original-button-ruling-1'));

    // Wait for iframe to appear
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe-ruling-1')).toBeInTheDocument();
    });

    const iframe = screen.getByTestId('original-document-iframe-ruling-1');
    expect(iframe).toHaveAttribute('sandbox', '');
    expect(iframe.getAttribute('srcdoc')).toBe(htmlContent);
    expect(iframe).toHaveAttribute('title', 'Original court document');

    // Button text should change to "Hide original"
    expect(screen.getByTestId('view-original-button-ruling-1')).toHaveTextContent('Hide original');

    fetchSpy.mockRestore();
  });

  it('hides iframe when "Hide original" is clicked', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const htmlContent = '<html><body><p>Content</p></body></html>';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(htmlContent, { status: 200 }),
    );

    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // Click "View original"
    fireEvent.click(screen.getByTestId('view-original-button-ruling-1'));

    // Wait for iframe to appear
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe-ruling-1')).toBeInTheDocument();
    });

    // Click "Hide original"
    fireEvent.click(screen.getByTestId('view-original-button-ruling-1'));

    // Iframe should be gone
    expect(screen.queryByTestId('original-document-iframe-ruling-1')).not.toBeInTheDocument();
    // Button should revert to "View original"
    expect(screen.getByTestId('view-original-button-ruling-1')).toHaveTextContent('View original');

    fetchSpy.mockRestore();
  });

  it('shows error message when document content fetch fails', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'Document not found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // Click "View original"
    fireEvent.click(screen.getByTestId('view-original-button-ruling-1'));

    // Wait for error message
    await waitFor(() => {
      expect(screen.getByTestId('viewer-error-ruling-1')).toBeInTheDocument();
    });

    expect(screen.getByTestId('viewer-error-ruling-1')).toHaveTextContent('Document not found');

    fetchSpy.mockRestore();
  });

  it('shows error message on network failure', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(
      new Error('Network error'),
    );

    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    // Click "View original"
    fireEvent.click(screen.getByTestId('view-original-button-ruling-1'));

    // Wait for error message
    await waitFor(() => {
      expect(screen.getByTestId('viewer-error-ruling-1')).toBeInTheDocument();
    });

    expect(screen.getByTestId('viewer-error-ruling-1')).toHaveTextContent('Failed to load document. Please try again.');

    fetchSpy.mockRestore();
  });

  it('supports independent viewer state for multiple ruling rows', async () => {
    const node1 = buildRulingNode({
      id: 'ruling-a',
      documentId: 'doc-html-a',
      documentFormat: 'html',
      hearingDate: '2026-03-10',
    });
    const node2 = buildRulingNode({
      id: 'ruling-b',
      documentId: 'doc-html-b',
      documentFormat: 'html',
      hearingDate: '2026-03-11',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node1, node2]));

    renderWithProvider(mocks);

    // Expand both rulings
    const buttons = await screen.findAllByLabelText(/Ruling from/, {}, { timeout: 3000 });
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);

    // Both View original buttons should appear
    expect(screen.getByTestId('view-original-button-ruling-a')).toBeInTheDocument();
    expect(screen.getByTestId('view-original-button-ruling-b')).toBeInTheDocument();
  });

  it('handles rapid toggle: load, hide, re-load without stale closure', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const htmlContent = '<html><body><p>Content</p></body></html>';

    // Mock two successful fetches (one for initial load, one for re-load)
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(htmlContent, { status: 200 }))
      .mockResolvedValueOnce(new Response(htmlContent, { status: 200 }));

    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    const btn = screen.getByTestId('view-original-button-ruling-1');

    // 1. Click "View original" — starts loading
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe-ruling-1')).toBeInTheDocument();
    });
    expect(btn).toHaveTextContent('Hide original');

    // 2. Click "Hide original" — toggles off
    fireEvent.click(btn);
    expect(screen.queryByTestId('original-document-iframe-ruling-1')).not.toBeInTheDocument();
    expect(btn).toHaveTextContent('View original');

    // 3. Click "View original" again — should load a second time, not stay stuck
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe-ruling-1')).toBeInTheDocument();
    });
    expect(btn).toHaveTextContent('Hide original');

    // Verify fetch was called twice (once per "View original" click)
    expect(fetchSpy).toHaveBeenCalledTimes(2);

    fetchSpy.mockRestore();
  });

  it('ignores duplicate clicks while loading (button is disabled)', async () => {
    const node = buildRulingNode({
      documentId: 'doc-html-1',
      documentFormat: 'html',
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const htmlContent = '<html><body><p>Content</p></body></html>';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(htmlContent, { status: 200 }),
    );

    renderWithProvider(mocks);

    // Expand the ruling
    await expandRuling(/Ruling from/);

    const btn = screen.getByTestId('view-original-button-ruling-1');

    // Fire two clicks back-to-back without awaiting — the button becomes
    // disabled after the first click triggers a re-render, so the second
    // click should be a no-op.
    fireEvent.click(btn);
    fireEvent.click(btn);

    // Wait for the document to load
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe-ruling-1')).toBeInTheDocument();
    });

    // Only one fetch should have been made
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    fetchSpy.mockRestore();
  });
});
