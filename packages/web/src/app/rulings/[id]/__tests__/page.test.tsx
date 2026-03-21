import { describe, it, expect, vi, beforeEach } from 'vitest';

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
});
