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

// Stub client-side component used inside the page
vi.mock('../RulingDetail', () => ({
  RulingDetail: () => null,
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
  // Metadata card entity links (#1515)
  // -------------------------------------------------------------------------

  it('renders judge name as a link to the judge profile page', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const judgeLink = screen.getByText('Johnson, Robert M.').closest('a');
    expect(judgeLink).toHaveAttribute('href', '/judges/judge-1');
  });

  it('renders case number as a link to the case detail page', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const caseLink = screen.getByText('25STCV12345').closest('a');
    expect(caseLink).toHaveAttribute('href', '/cases/case-1');
  });

  it('renders county as a link to the rulings feed filtered by county', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const countyLink = screen.getByText('Los Angeles').closest('a');
    expect(countyLink).toHaveAttribute('href', '/rulings?county=Los%20Angeles');
  });

  it('renders court name as a link to the rulings feed filtered by county', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { ruling: FULL_RULING },
    });

    const jsx = await RulingDetailPage({ params: { id: 'ruling-1' } });
    render(jsx);

    const courtLink = screen.getByText('Los Angeles Superior Court').closest('a');
    expect(courtLink).toHaveAttribute('href', '/rulings?county=Los%20Angeles');
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

    // No entity links should exist in the metadata card
    expect(screen.queryByText('Johnson, Robert M.')).not.toBeInTheDocument();
    expect(screen.queryByText('25STCV12345')).not.toBeInTheDocument();
    expect(screen.queryByText('Los Angeles')).not.toBeInTheDocument();
    expect(screen.queryByText('Los Angeles Superior Court')).not.toBeInTheDocument();
  });
});
