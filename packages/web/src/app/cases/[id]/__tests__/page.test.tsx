import { describe, it, expect, vi, beforeEach } from 'vitest';

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

// ---------------------------------------------------------------------------
// Import the page under test (must come after mocks)
// ---------------------------------------------------------------------------

import CaseDetailPage from '../page';

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
});
