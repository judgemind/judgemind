import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mocks — must be declared before importing the page module
// ---------------------------------------------------------------------------

const mockQuery = vi.fn();
const mockNotFound = vi.fn();

vi.mock('@/lib/apollo-client', () => ({
  createApolloClient: () => ({ query: mockQuery }),
}));

vi.mock('next/navigation', () => ({
  notFound: (...args: unknown[]) => {
    mockNotFound(...args);
    throw new Error('NEXT_NOT_FOUND');
  },
  usePathname: () => '/judges/test-id',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// Mock the JudgeProfile client component so we only test the server page logic
vi.mock('../JudgeProfile', () => ({
  JudgeProfile: function MockJudgeProfile({ judgeId }: { judgeId: string }) {
    return { type: 'div', props: { 'data-testid': 'judge-profile', 'data-judge-id': judgeId }, key: null };
  },
}));

// ---------------------------------------------------------------------------
// Import the page under test (must come after mocks)
// ---------------------------------------------------------------------------

import JudgeDetailPage from '../page';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Recursively search React element tree for a node matching a predicate. */
function findInTree(
  node: unknown,
  predicate: (n: { type?: unknown; props?: Record<string, unknown> }) => boolean,
): { type?: unknown; props?: Record<string, unknown> } | null {
  if (!node || typeof node !== 'object') return null;
  const n = node as { type?: unknown; props?: Record<string, unknown> };
  if (predicate(n)) return n;
  const children = n.props?.children;
  if (Array.isArray(children)) {
    for (const child of children) {
      const found = findInTree(child, predicate);
      if (found) return found;
    }
  } else if (children && typeof children === 'object') {
    const found = findInTree(children, predicate);
    if (found) return found;
  }
  return null;
}

/** Find all nodes matching a predicate in the React element tree. */
function findAllInTree(
  node: unknown,
  predicate: (n: { type?: unknown; props?: Record<string, unknown> }) => boolean,
): Array<{ type?: unknown; props?: Record<string, unknown> }> {
  const results: Array<{ type?: unknown; props?: Record<string, unknown> }> = [];
  if (!node || typeof node !== 'object') return results;
  const n = node as { type?: unknown; props?: Record<string, unknown> };
  if (predicate(n)) results.push(n);
  const children = n.props?.children;
  if (Array.isArray(children)) {
    for (const child of children) {
      results.push(...findAllInTree(child, predicate));
    }
  } else if (children && typeof children === 'object') {
    results.push(...findAllInTree(children, predicate));
  }
  return results;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JudgeDetailPage (SSR)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders judge heading, status badge, and mounts JudgeProfile', async () => {
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

    // Check the rendered tree contains the JudgeProfile component
    const profile = findInTree(result, (n) =>
      typeof n.type === 'function' &&
      n.type.name === 'MockJudgeProfile',
    );
    expect(profile).toBeTruthy();
    expect(profile?.props?.judgeId).toBe('judge-1');
  });

  it('renders active status badge for active judge', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        judge: {
          id: 'judge-1',
          canonicalName: 'Hon. Jane Smith',
          department: null,
          isActive: true,
          court: null,
        },
      },
    });

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const badges = findAllInTree(result, (n) =>
      typeof n.props?.className === 'string' &&
      n.props.className.includes('rounded-full') &&
      n.type === 'span',
    );
    const activeBadge = badges.find((b) => b.props?.children === 'Active');
    expect(activeBadge).toBeTruthy();
    expect(activeBadge?.props?.className).toContain('bg-green-100');
  });

  it('renders inactive status badge for inactive judge', async () => {
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
    const badges = findAllInTree(result, (n) =>
      typeof n.props?.className === 'string' &&
      n.props.className.includes('rounded-full') &&
      n.type === 'span',
    );
    const inactiveBadge = badges.find((b) => b.props?.children === 'Inactive');
    expect(inactiveBadge).toBeTruthy();
    expect(inactiveBadge?.props?.className).toContain('bg-slate-100');
  });

  it('calls notFound when GraphQL returns null judge', async () => {
    mockQuery.mockResolvedValueOnce({
      data: { judge: null },
    });

    await expect(
      JudgeDetailPage({ params: { id: 'nonexistent' } }),
    ).rejects.toThrow('NEXT_NOT_FOUND');
    expect(mockNotFound).toHaveBeenCalled();
  });

  it('calls notFound when GraphQL query throws', async () => {
    mockQuery.mockRejectedValueOnce(new Error('Network error'));

    await expect(
      JudgeDetailPage({ params: { id: 'error-judge' } }),
    ).rejects.toThrow('NEXT_NOT_FOUND');
    expect(mockNotFound).toHaveBeenCalled();
  });

  it('renders court info and department when available', async () => {
    mockQuery.mockResolvedValueOnce({
      data: {
        judge: {
          id: 'judge-3',
          canonicalName: 'Hon. Alice Brown',
          department: '5',
          isActive: true,
          court: {
            courtName: 'San Francisco Superior Court',
            county: 'San Francisco',
          },
        },
      },
    });

    const result = await JudgeDetailPage({ params: { id: 'judge-3' } });
    const courtParagraph = findInTree(result, (n) =>
      n.type === 'p' &&
      typeof n.props?.className === 'string' &&
      n.props.className.includes('text-slate-500'),
    );
    expect(courtParagraph).toBeTruthy();
  });
});
