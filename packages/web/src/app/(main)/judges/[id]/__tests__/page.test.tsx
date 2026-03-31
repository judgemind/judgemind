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

// ---------------------------------------------------------------------------
// Test data factory
// ---------------------------------------------------------------------------

function mockJudgeData(overrides: Partial<{
  id: string;
  canonicalName: string;
  department: string | null;
  isActive: boolean;
  court: { courtName: string; county: string } | null;
  assignments: Array<{
    department: string;
    caseType: string | null;
    courthouse: string | null;
    firstSeen: string;
    lastSeen: string;
  }>;
}> = {}) {
  return {
    data: {
      judge: {
        id: overrides.id ?? 'judge-1',
        canonicalName: overrides.canonicalName ?? 'Hon. Jane Smith',
        department: overrides.department ?? null,
        isActive: overrides.isActive ?? true,
        court: overrides.court ?? null,
        assignments: overrides.assignments ?? [],
      },
    },
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JudgeDetailPage (SSR)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders judge heading and mounts JudgeProfile', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      court: { courtName: 'Los Angeles Superior Court', county: 'Los Angeles' },
      assignments: [{ department: '12', caseType: 'civil', courthouse: 'Stanley Mosk Courthouse', firstSeen: '2024-01-01', lastSeen: '2026-03-01' }],
    }));

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

  it('does not render badge for active judge', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({ isActive: true }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const activeBadge = findInTree(result, (n) => n.props?.children === 'Active');
    expect(activeBadge).toBeNull();
    const inactiveBadge = findInTree(result, (n) => n.props?.children === 'Inactive');
    expect(inactiveBadge).toBeNull();
  });

  it('renders inactive status Badge for inactive judge', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({ id: 'judge-2', canonicalName: 'Hon. John Doe', isActive: false }));

    const result = await JudgeDetailPage({ params: { id: 'judge-2' } });
    const inactiveBadge = findInTree(result, (n) => n.props?.children === 'Inactive');
    expect(inactiveBadge).toBeTruthy();
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
    mockQuery.mockResolvedValueOnce(Promise.reject(new Error('Network error')));

    await expect(
      JudgeDetailPage({ params: { id: 'error-judge' } }),
    ).rejects.toThrow('NEXT_NOT_FOUND');
    expect(mockNotFound).toHaveBeenCalled();
  });

  it('renders court info when available', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      id: 'judge-3',
      canonicalName: 'Hon. Alice Brown',
      court: { courtName: 'San Francisco Superior Court', county: 'San Francisco' },
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-3' } });
    const courtParagraph = findInTree(result, (n) =>
      n.type === 'p' &&
      typeof n.props?.className === 'string' &&
      n.props.className.includes('text-muted-foreground'),
    );
    expect(courtParagraph).toBeTruthy();
  });

  it('renders a breadcrumb with Judges link and judge name', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      court: { courtName: 'Los Angeles Superior Court', county: 'Los Angeles' },
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    // Find the breadcrumb nav element
    const breadcrumb = findInTree(result, (n) =>
      n.type === 'nav' &&
      n.props?.['aria-label'] === 'Breadcrumb',
    );
    expect(breadcrumb).toBeTruthy();

    // Find the Judges link inside the breadcrumb (Link may be a function or forwardRef object)
    const judgesLink = findInTree(breadcrumb, (n) =>
      n.props?.href === '/judges',
    );
    expect(judgesLink).toBeTruthy();

    // Find the judge name text in the breadcrumb
    const judgeName = findInTree(breadcrumb, (n) =>
      n.type === 'span' &&
      typeof n.props?.className === 'string' &&
      n.props.className.includes('text-foreground') &&
      n.props?.children === 'Hon. Jane Smith',
    );
    expect(judgeName).toBeTruthy();
  });

  it('does not wrap profile header in a Card component', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      court: { courtName: 'Los Angeles Superior Court', county: 'Los Angeles' },
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    // No Card component should be present in the header area
    const card = findInTree(result, (n) =>
      typeof n.type === 'object' &&
      n.type !== null &&
      'displayName' in n.type &&
      (n.type as { displayName?: string }).displayName === 'Card',
    );
    expect(card).toBeNull();
  });

  // ---------------------------------------------------------------------------
  // New tests: assignment metadata in header
  // ---------------------------------------------------------------------------

  it('renders current assignment metadata (department, case type, courthouse) in header', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      court: { courtName: 'Superior Court', county: 'Orange' },
      assignments: [
        { department: 'C24', caseType: 'civil', courthouse: 'Central Justice Center, Santa Ana', firstSeen: '2024-01-01', lastSeen: '2026-03-01' },
      ],
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const metadataEl = findInTree(result, (n) =>
      n.props?.['data-testid'] === 'assignment-metadata',
    );
    expect(metadataEl).toBeTruthy();
    // Should contain the department, case type, and courthouse joined by middot
    const text = String(metadataEl?.props?.children ?? '');
    expect(text).toContain('Department C24');
    expect(text).toContain('Civil');
    expect(text).toContain('Central Justice Center');
  });

  it('renders Previously section for judges with multiple assignments', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      canonicalName: 'Kalra, Upinder S.',
      court: { courtName: 'Los Angeles Superior Court', county: 'Los Angeles' },
      assignments: [
        { department: '19', caseType: 'civil', courthouse: 'Stanley Mosk Courthouse', firstSeen: '2025-06-01', lastSeen: '2026-03-01' },
        { department: '51', caseType: 'civil', courthouse: 'Stanley Mosk Courthouse', firstSeen: '2024-01-15', lastSeen: '2025-05-31' },
      ],
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const previousSection = findInTree(result, (n) =>
      n.props?.['data-testid'] === 'previously-section',
    );
    expect(previousSection).toBeTruthy();

    // Should contain the previous department
    const prevItems = findAllInTree(previousSection!, (n) =>
      n.type === 'p' &&
      typeof n.props?.className === 'string' &&
      n.props.className.includes('text-muted-foreground'),
    );
    expect(prevItems.length).toBeGreaterThanOrEqual(1);
  });

  it('does not render Previously section for judge with single assignment', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      court: { courtName: 'Los Angeles Superior Court', county: 'Los Angeles' },
      assignments: [
        { department: '51', caseType: 'civil', courthouse: 'Stanley Mosk Courthouse', firstSeen: '2024-01-01', lastSeen: '2026-03-01' },
      ],
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const previousSection = findInTree(result, (n) =>
      n.props?.['data-testid'] === 'previously-section',
    );
    expect(previousSection).toBeNull();
  });

  it('does not render Previously section for judge with no assignments', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      court: { courtName: 'Los Angeles Superior Court', county: 'Los Angeles' },
      assignments: [],
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const previousSection = findInTree(result, (n) =>
      n.props?.['data-testid'] === 'previously-section',
    );
    expect(previousSection).toBeNull();
  });

  it('falls back to judges.department when no assignments exist', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      department: '5',
      court: { courtName: 'San Francisco Superior Court', county: 'San Francisco' },
      assignments: [],
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const metadataEl = findInTree(result, (n) =>
      n.props?.['data-testid'] === 'assignment-metadata',
    );
    expect(metadataEl).toBeTruthy();
    const text = String(metadataEl?.props?.children ?? '');
    expect(text).toContain('Dept. 5');
  });

  it('does not render assignment metadata when no department data exists', async () => {
    mockQuery.mockResolvedValueOnce(mockJudgeData({
      department: null,
      court: { courtName: 'Superior Court', county: 'Los Angeles' },
      assignments: [],
    }));

    const result = await JudgeDetailPage({ params: { id: 'judge-1' } });
    const metadataEl = findInTree(result, (n) =>
      n.props?.['data-testid'] === 'assignment-metadata',
    );
    expect(metadataEl).toBeNull();
  });
});
