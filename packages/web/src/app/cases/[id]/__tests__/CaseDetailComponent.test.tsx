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
      judges {
        id
        canonicalName
        department
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
      filedAt: '2025-01-15',
      court: {
        courtName: 'Los Angeles Superior Court',
        county: 'Los Angeles',
      },
      judges: [
        { id: 'judge-1', canonicalName: 'Johnson, Robert M.', department: '12' },
      ],
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

// ---------------------------------------------------------------------------
// Tests
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

    // Wait for GraphQL data to resolve
    const grantedEl = await screen.findByText('GRANTED', {}, { timeout: 3000 });
    expect(grantedEl).toBeInTheDocument();

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

    // Wait for data
    await screen.findByText('The motion is granted.', {}, { timeout: 3000 });

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

    await screen.findByText('Safe content', {}, { timeout: 3000 });

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

    await screen.findByText('Content', {}, { timeout: 3000 });

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).not.toContain('onclick');
    expect(rulingContent?.innerHTML).not.toContain('onerror');
  });

  it('shows formatted HTML with CSS truncation when collapsed for long content', async () => {
    // Create a long ruling text that exceeds RULING_TEXT_TRUNCATE_LENGTH (500)
    const longText = 'A'.repeat(600);
    const node = buildRulingNode({
      rulingTextHtml: `<p>${longText}</p>`,
      rulingText: longText,
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Wait for data — when collapsed, should show HTML with max-height truncation
    await screen.findByText('Show more', {}, { timeout: 3000 });

    // Should render formatted HTML even when collapsed (CSS truncation, not text truncation)
    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.classList.contains('max-h-40')).toBe(true);
    expect(rulingContent?.classList.contains('overflow-hidden')).toBe(true);

    // Should show a fade-out gradient overlay
    const gradient = container.querySelector('.bg-gradient-to-t');
    expect(gradient).toBeInTheDocument();
  });

  it('shows formatted HTML without truncation when expanded for long content', async () => {
    const longText = 'A'.repeat(600);
    const node = buildRulingNode({
      rulingTextHtml: `<p><strong>${longText}</strong></p>`,
      rulingText: longText,
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    // Wait for the Show more button
    const showMoreButton = await screen.findByText('Show more', {}, { timeout: 3000 });

    // Click to expand
    fireEvent.click(showMoreButton);

    // Now it should show formatted HTML without max-height restriction
    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('<strong>');
    expect(rulingContent?.classList.contains('max-h-40')).toBe(false);

    // Fade gradient should be gone
    expect(container.querySelector('.bg-gradient-to-t')).not.toBeInTheDocument();
  });

  it('renders only rulingTextHtml when rulingText is null', async () => {
    const node = buildRulingNode({
      rulingTextHtml: '<p>Only HTML available</p>',
      rulingText: null,
    });
    const mocks = buildMocks(buildCaseData(), buildRulingsData([node]));
    const { container } = renderWithProvider(mocks);

    await screen.findByText('Only HTML available', {}, { timeout: 3000 });

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

    // Wait for case data to load — CaseDetail renders court/judge info, not caseNumber
    await screen.findByText('Los Angeles Superior Court', {}, { timeout: 3000 });

    // No ruling content should be rendered
    expect(container.querySelector('.ruling-content')).not.toBeInTheDocument();
  });
});
