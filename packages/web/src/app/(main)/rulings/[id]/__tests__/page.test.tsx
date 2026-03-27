import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks — must be declared before importing the page module
// ---------------------------------------------------------------------------

const mockQuery = vi.fn();

vi.mock('@/lib/apollo-client', () => ({
  createApolloClient: () => ({ query: mockQuery }),
}));

// next/navigation: notFound() throws a special error to signal a 404.
class NotFoundError extends Error {
  readonly digest = 'NEXT_NOT_FOUND';
  constructor() {
    super('NEXT_NOT_FOUND');
  }
}

vi.mock('next/navigation', () => ({
  notFound: () => {
    throw new NotFoundError();
  },
  usePathname: () => '/rulings/test-id',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// Stub sanitize-html — the server component calls sanitizeRulingHtml
// before passing HTML to the client component
vi.mock('@/lib/sanitize-html', () => ({
  sanitizeRulingHtml: (html: string) => html,
}));

// Mock next/link — render as a plain anchor for testability
vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

// Stub client-side components used inside the page
vi.mock('../RulingDetail', () => ({
  RulingDetail: () => null,
}));

vi.mock('../SiblingRulings', () => ({
  SiblingRulings: () => null,
}));

// ---------------------------------------------------------------------------
// Import the page under test (must come after mocks)
// ---------------------------------------------------------------------------

import RulingDetailPage from '../page';

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

const FULL_RULING = {
  id: 'ruling-1',
  hearingDate: '2026-03-10',
  outcome: 'granted',
  motionType: 'msj',
  isTentative: true,
  department: '12',
  rulingText: 'The motion is granted.',
  rulingTextHtml: null,
  summary: 'Court grants MSJ.',
  postedAt: '2026-03-09T18:00:00Z',
  documentId: 'doc-1',
  documentFormat: 'pdf',
  case: {
    id: 'case-1',
    caseNumber: '25STCV12345',
    caseTitle: 'Smith v. Jones',
    caseType: 'civil',
    parties: [
      { id: 'p1', canonicalName: 'Smith, John', partyType: 'individual', role: 'plaintiff' },
      { id: 'p2', canonicalName: 'Jones, Jane', partyType: 'individual', role: 'defendant' },
    ],
  },
  judge: {
    id: 'judge-1',
    canonicalName: 'Johnson, Robert M.',
  },
  court: {
    courtName: 'Los Angeles Superior Court',
    county: 'Los Angeles',
  },
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RulingDetailPage (SSR smoke)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders without throwing when GraphQL returns valid ruling data', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const result = await RulingDetailPage({ params: { id: 'ruling-1' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('renders with minimal ruling data (null optional fields)', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          id: 'ruling-2',
          hearingDate: '2026-03-10',
          outcome: null,
          motionType: null,
          isTentative: false,
          department: null,
          rulingText: null,
          rulingTextHtml: null,
          summary: null,
          postedAt: null,
          documentId: null,
          documentFormat: null,
          case: null,
          judge: null,
          court: null,
        },
      },
    });

    const result = await RulingDetailPage({ params: { id: 'ruling-2' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('calls notFound() when GraphQL returns null ruling', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: null },
    });

    await expect(
      RulingDetailPage({ params: { id: 'nonexistent' } }),
    ).rejects.toThrow('NEXT_NOT_FOUND');
  });

  it('calls notFound() when GraphQL query throws', async () => {
    mockQuery.mockRejectedValueOnce(new Error('Network error'));

    await expect(
      RulingDetailPage({ params: { id: 'error-ruling' } }),
    ).rejects.toThrow('NEXT_NOT_FOUND');
  });

  it('renders when ruling has rulingTextHtml', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          rulingTextHtml: '<p>The motion is <strong>granted</strong>.</p>',
        },
      },
    });

    const result = await RulingDetailPage({ params: { id: 'ruling-1' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('degrades gracefully when sanitization throws', async () => {
    // Override the mock to throw
    const sanitizeMod = await import('@/lib/sanitize-html');
    vi.spyOn(sanitizeMod, 'sanitizeRulingHtml').mockImplementation(() => {
      throw new Error('jsdom not available');
    });

    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          rulingTextHtml: '<p>Some HTML</p>',
        },
      },
    });

    // Should NOT throw — the try/catch should handle the sanitization error
    const result = await RulingDetailPage({ params: { id: 'ruling-1' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  // -------------------------------------------------------------------------
  // Header subtitle entity links (#1515, consolidated in #1643)
  // -------------------------------------------------------------------------

  it('renders judge name as a link to the judge profile page in the subtitle', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const judgeLink = screen.getByText('Johnson, Robert M.').closest('a');
    expect(judgeLink).toHaveAttribute('href', '/judges/judge-1');
  });

  it('renders case number as a link to the case detail page in the subtitle', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // Case number now has "Case " prefix in the subtitle
    const caseLink = screen.getByText(/25STCV12345/).closest('a');
    expect(caseLink).toHaveAttribute('href', '/cases/case-1');
  });

  it('renders court name as a link to the rulings feed filtered by county in the subtitle', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const courtLink = screen.getByText('Los Angeles Superior Court').closest('a');
    expect(courtLink).toHaveAttribute('href', '/rulings?county=Los%20Angeles');
  });

  it('does not display county as a separate field from court (#1643)', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // "Los Angeles" as county is part of the court name "Los Angeles Superior Court",
    // so it should not appear as a separate standalone text node.
    // The court name link should exist, but there should be no separate "County" label.
    const allDts = document.querySelectorAll('dt');
    const dtTexts = Array.from(allDts).map((dt) => dt.textContent);
    expect(dtTexts).not.toContain('County');
  });

  it('appends county name when not contained in court name', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          court: {
            courtName: 'Superior Court',
            county: 'San Diego',
          },
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // When court name doesn't contain the county, both should appear in the link
    const courtLink = screen.getByText(/Superior Court/).closest('a');
    expect(courtLink).toHaveAttribute('href', '/rulings?county=San%20Diego');
    expect(courtLink?.textContent).toContain('San Diego');
  });

  it('does not append county when court name already contains it', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          court: {
            courtName: 'Superior Court, County of San Diego',
            county: 'San Diego',
          },
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const courtLink = screen.getByText('Superior Court, County of San Diego').closest('a');
    expect(courtLink).toHaveAttribute('href', '/rulings?county=San%20Diego');
    // Should not duplicate "San Diego" — the county is already in the court name
    expect(courtLink?.textContent).toBe('Superior Court, County of San Diego');
  });

  it('renders hearing date in the subtitle', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // Hearing date should appear in the subtitle, not in the metadata card
    expect(screen.getByText(/Mar(ch)?\s+10,\s+2026/)).toBeInTheDocument();
    // No "Hearing Date" label in the metadata card
    const allDts = document.querySelectorAll('dt');
    const dtTexts = Array.from(allDts).map((dt) => dt.textContent);
    expect(dtTexts).not.toContain('Hearing Date');
  });

  it('renders department inline in the subtitle when present', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // Department should appear as "Dept. 12" inline in the subtitle
    expect(screen.getByText('Dept. 12')).toBeInTheDocument();

    // No standalone department card (no <dt> elements for Department)
    const allDts = document.querySelectorAll('dt');
    const dtTexts = Array.from(allDts).map((dt) => dt.textContent);
    expect(dtTexts).not.toContain('Department');
  });

  it('does not render department text when department is null', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          department: null,
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // No "Dept." text should appear anywhere
    expect(screen.queryByText(/Dept\./)).not.toBeInTheDocument();
  });

  it('renders department inline even without judge or court', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          judge: null,
          court: null,
          department: 'C',
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // Department should still appear when judge and court are null
    expect(screen.getByText('Dept. C')).toBeInTheDocument();
  });

  it('does not render standalone department card (removed)', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // No <dt> elements should exist — the standalone metadata card is removed
    const allDts = document.querySelectorAll('dt');
    expect(allDts).toHaveLength(0);
  });

  it('does not render entity links when judge, case, and court are null', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          judge: null,
          case: null,
          court: null,
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // No entity links should exist in the subtitle
    expect(screen.queryByText('Johnson, Robert M.')).not.toBeInTheDocument();
    expect(screen.queryByText(/25STCV12345/)).not.toBeInTheDocument();
    expect(screen.queryByText('Los Angeles Superior Court')).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Case type badge (#1988)
  // -------------------------------------------------------------------------

  it('renders case type badge when caseType is available', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const badge = screen.getByTestId('case-type-badge');
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveTextContent('Civil');
  });

  it('does not render case type badge when caseType is null', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          case: {
            ...FULL_RULING.case,
            caseType: null,
          },
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    expect(screen.queryByTestId('case-type-badge')).not.toBeInTheDocument();
  });

  it('does not render case type badge when case is null', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          case: null,
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    expect(screen.queryByTestId('case-type-badge')).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Parties section (#1988)
  // -------------------------------------------------------------------------

  it('renders parties section when parties are available', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const partiesSection = screen.getByTestId('parties-section');
    expect(partiesSection).toBeInTheDocument();
    expect(screen.getByText('Plaintiffs')).toBeInTheDocument();
    expect(screen.getByText('Defendants')).toBeInTheDocument();
    expect(screen.getByText('Smith, John')).toBeInTheDocument();
    expect(screen.getByText('Jones, Jane')).toBeInTheDocument();
  });

  it('does not render parties section when parties array is empty', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          case: {
            ...FULL_RULING.case,
            parties: [],
          },
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    expect(screen.queryByTestId('parties-section')).not.toBeInTheDocument();
  });

  it('does not render parties section when case is null', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        ruling: {
          ...FULL_RULING,
          case: null,
        },
      },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    expect(screen.queryByTestId('parties-section')).not.toBeInTheDocument();
  });

  it('shows party type when available', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    // partyType "individual" should appear as "(Individual)" via formatLabel
    expect(screen.getAllByText('(Individual)')).toHaveLength(2);
  });
});
