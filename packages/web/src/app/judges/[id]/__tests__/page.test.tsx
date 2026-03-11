import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mocks — must be declared before importing the page module
// ---------------------------------------------------------------------------

const mockQuery = vi.fn();

vi.mock('@/lib/apollo-client', () => ({
  createApolloClient: () => ({ query: mockQuery }),
}));

vi.mock('next/navigation', () => ({
  notFound: vi.fn(),
  usePathname: () => '/judges/test-id',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// ---------------------------------------------------------------------------
// Import the page under test (must come after mocks)
// ---------------------------------------------------------------------------

import JudgeDetailPage from '../page';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JudgeDetailPage (SSR smoke)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders without throwing when GraphQL returns valid judge data', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        judge: {
          id: 'judge-1',
          canonicalName: 'Hon. Jane Smith',
          department: '12',
          isActive: true,
          court: {
            courtName: 'Los Angeles Superior Court',
            county: 'Los Angeles',
          },
        },
      },
    });

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('renders with minimal judge data (null optional fields)', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        judge: {
          id: 'judge-2',
          canonicalName: 'Hon. John Doe',
          department: null,
          isActive: false,
          court: null,
        },
      },
    });

    const result = await JudgeDetailPage({ params: { id: 'judge-2' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('renders fallback heading when GraphQL returns null judge', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { judge: null },
    });

    // The judge page does NOT call notFound() — it renders a fallback heading
    const result = await JudgeDetailPage({ params: { id: 'nonexistent' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });

  it('renders fallback heading when GraphQL query throws', async () => {
    mockQuery.mockRejectedValueOnce(new Error('Network error'));

    // The judge page catches errors and falls through to fallback display
    const result = await JudgeDetailPage({ params: { id: 'error-judge' } });
    expect(result).toBeTruthy();
    expect(result.type).toBe('div');
  });
});
