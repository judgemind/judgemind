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
// We replicate that behaviour so the test can detect the call.
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
  usePathname: () => '/cases/test-id',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// Stub client-side component used inside the page
vi.mock('../CaseDetail', () => ({
  CaseDetail: () => null,
}));

// Mock next/link as a plain anchor
vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

// ---------------------------------------------------------------------------
// Import the page under test (must come after mocks)
// ---------------------------------------------------------------------------

import CaseDetailPage from '../page';

// ---------------------------------------------------------------------------
// Helper: render the async server component
// ---------------------------------------------------------------------------

async function renderPage(id: string) {
  const jsx = await CaseDetailPage({ params: { id } });
  return render(jsx);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CaseDetailPage (SSR smoke)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders without throwing when GraphQL returns valid case data', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        case: {
          id: 'case-1',
          caseNumber: '25STCV12345',
          caseTitle: 'Smith v. Jones',
          caseType: 'civil',
          caseStatus: 'active',
          court: {
            courtName: 'Los Angeles Superior Court',
            county: 'Los Angeles',
          },
          judges: [
            { id: 'judge-1', canonicalName: 'Johnson, Robert M.', department: '12' },
          ],
        },
      },
    });

    const result = await CaseDetailPage({ params: { id: 'case-1' } });
    // The page should return a valid React element (JSX)
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('renders with minimal case data (null optional fields)', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        case: {
          id: 'case-2',
          caseNumber: '25STCV99999',
          caseTitle: null,
          caseType: null,
          caseStatus: null,
          court: null,
          judges: [],
        },
      },
    });

    const result = await CaseDetailPage({ params: { id: 'case-2' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('calls notFound() when GraphQL returns null case', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { case: null },
    });

    await expect(
      CaseDetailPage({ params: { id: 'nonexistent' } }),
    ).rejects.toThrow('NEXT_NOT_FOUND');
  });

  it('calls notFound() when GraphQL query throws', async () => {
    mockQuery.mockRejectedValueOnce(new Error('Network error'));

    await expect(
      CaseDetailPage({ params: { id: 'error-case' } }),
    ).rejects.toThrow('NEXT_NOT_FOUND');
  });

  it('includes judge names in the rendered output', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        case: {
          id: 'case-3',
          caseNumber: '25HR054887C',
          caseTitle: 'Turner vs Decker',
          caseType: 'civil',
          caseStatus: 'active',
          court: {
            courtName: 'Superior Court',
            county: 'San Diego',
          },
          judges: [
            { id: 'judge-reid', canonicalName: 'Chandra Reid', department: 'C21' },
          ],
        },
      },
    });

    await renderPage('case-3');
    expect(screen.getByText(/Judge Chandra Reid/)).toBeInTheDocument();
    expect(screen.getByText(/Dept\. C21/)).toBeInTheDocument();
    // Judge link should point to judge detail page
    const judgeLink = screen.getByText(/Judge Chandra Reid/).closest('a');
    expect(judgeLink?.getAttribute('href')).toBe('/judges/judge-reid');
  });

  it('renders multiple judges', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        case: {
          id: 'case-5',
          caseNumber: '25STCV00002',
          caseTitle: null,
          caseType: null,
          caseStatus: null,
          court: null,
          judges: [
            { id: 'judge-a', canonicalName: 'Judge A', department: null },
            { id: 'judge-b', canonicalName: 'Judge B', department: '5' },
          ],
        },
      },
    });

    await renderPage('case-5');
    expect(screen.getByText(/Judge Judge A/)).toBeInTheDocument();
    expect(screen.getByText(/Judge Judge B/)).toBeInTheDocument();
    // Both judge links should be present
    const linkA = screen.getByText(/Judge Judge A/).closest('a');
    expect(linkA?.getAttribute('href')).toBe('/judges/judge-a');
    const linkB = screen.getByText(/Judge Judge B/).closest('a');
    expect(linkB?.getAttribute('href')).toBe('/judges/judge-b');
  });

  it('does not render judges line when no judges', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        case: {
          id: 'case-6',
          caseNumber: '25STCV00003',
          caseTitle: null,
          caseType: null,
          caseStatus: null,
          court: null,
          judges: [],
        },
      },
    });

    await renderPage('case-6');
    expect(screen.queryByText(/Judge /)).not.toBeInTheDocument();
  });
});
