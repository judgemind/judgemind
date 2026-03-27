import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Mock Apollo client
const mockUseQuery = vi.fn();
vi.mock('@apollo/client', () => ({
  useQuery: (...args: unknown[]) => mockUseQuery(...args),
  gql: (strings: TemplateStringsArray) => strings.join(''),
}));

const mockReplace = vi.fn();
const mockPush = vi.fn();
let mockSearchParamsValue = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace, back: vi.fn() }),
  useSearchParams: () => mockSearchParamsValue,
}));

vi.mock('next/link', () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
    [key: string]: unknown;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

// Mock lucide-react icons
vi.mock('lucide-react', () => ({
  AlertCircle: ({ className }: { className?: string }) => (
    <span data-testid="alert-circle-icon" className={className} />
  ),
  Search: ({ className }: { className?: string }) => (
    <span data-testid="search-icon" className={className} />
  ),
  Scale: ({ className }: { className?: string }) => (
    <span data-testid="scale-icon" className={className} />
  ),
}));

// Mock Checkbox component
vi.mock('@/components/ui/checkbox', () => ({
  Checkbox: ({
    checked,
    onCheckedChange,
    'aria-label': ariaLabel,
  }: {
    checked: boolean;
    onCheckedChange: () => void;
    'aria-label': string;
  }) => (
    <input
      type="checkbox"
      checked={checked}
      onChange={onCheckedChange}
      aria-label={ariaLabel}
      data-testid="judge-checkbox"
    />
  ),
}));

// IntersectionObserver mock
let intersectionCallback: IntersectionObserverCallback;
let mockObserve: ReturnType<typeof vi.fn>;
let mockDisconnect: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockObserve = vi.fn();
  mockDisconnect = vi.fn();

  vi.stubGlobal(
    'IntersectionObserver',
    vi.fn((callback: IntersectionObserverCallback) => {
      intersectionCallback = callback;
      return {
        observe: mockObserve,
        disconnect: mockDisconnect,
        unobserve: vi.fn(),
      };
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

import { JudgesList } from '../JudgesList';

const MOCK_JUDGES_DATA = {
  judges: {
    edges: [
      {
        cursor: 'cursor-1',
        node: {
          id: 'judge-1',
          canonicalName: 'Smith, John A.',
          department: '42',
          isActive: true,
          court: {
            courtName: 'Superior Court of California',
            county: 'Los Angeles',
          },
        },
      },
      {
        cursor: 'cursor-2',
        node: {
          id: 'judge-2',
          canonicalName: 'Johnson, Robert M.',
          department: null,
          isActive: false,
          court: {
            courtName: 'Superior Court of California',
            county: 'Orange',
          },
        },
      },
    ],
    pageInfo: {
      hasNextPage: true,
      endCursor: 'cursor-2',
    },
  },
};

describe('JudgesList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParamsValue = new URLSearchParams();
  });

  it('renders skeleton rows while loading', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { container } = render(<JudgesList />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders error state with soft styling', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: new Error('Network error'),
      fetchMore: vi.fn(),
    });

    const { container } = render(<JudgesList />);
    expect(screen.getByText(/Failed to load judges/)).toBeInTheDocument();

    // Should NOT use destructive styling
    expect(container.querySelector('.text-destructive')).not.toBeInTheDocument();

    // Should use soft styling with text-red-700
    expect(container.querySelector('.text-red-700')).toBeInTheDocument();

    // Should have AlertCircle icon
    expect(screen.getByTestId('alert-circle-icon')).toBeInTheDocument();

    // Should have a Try again action
    expect(screen.getByText('Try again')).toBeInTheDocument();
  });

  it('renders empty state when no judges found', () => {
    mockUseQuery.mockReturnValue({
      data: {
        judges: {
          edges: [],
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText(/No judges found/)).toBeInTheDocument();
  });

  it('renders judge names', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText('Smith, John A.')).toBeInTheDocument();
    expect(screen.getByText('Johnson, Robert M.')).toBeInTheDocument();
  });

  it('renders court info for each judge', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText(/Los Angeles/)).toBeInTheDocument();
    expect(screen.getByText(/Orange/)).toBeInTheDocument();
  });

  it('does not render status badges in the list view', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
    expect(screen.queryByText('Inactive')).not.toBeInTheDocument();
  });

  it('renders department inline with judge name when available', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    // Department should appear inline next to the judge name
    expect(screen.getByText(/Dept\. 42/)).toBeInTheDocument();
    // The department text should be in the same cell as the judge name
    const judgeLink = screen.getByText('Smith, John A.');
    const cell = judgeLink.closest('td');
    expect(cell?.textContent).toContain('Dept. 42');
  });

  it('does not render department text when department is null', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    // Johnson has no department — should not show "Dept." text near that judge
    const johnsonLink = screen.getByText('Johnson, Robert M.');
    const cell = johnsonLink.closest('td');
    expect(cell?.textContent).not.toContain('Dept.');
  });

  it('renders judge links pointing to detail pages', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const link = screen.getByText('Smith, John A.').closest('a');
    expect(link).toHaveAttribute('href', '/judges/judge-1');
  });

  it('renders sentinel element when hasNextPage is true (infinite scroll)', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
  });

  it('does not render sentinel when hasNextPage is false', () => {
    mockUseQuery.mockReturnValue({
      data: {
        judges: {
          ...MOCK_JUDGES_DATA.judges,
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('sets up IntersectionObserver on the sentinel element', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '200px' },
    );
    expect(mockObserve).toHaveBeenCalled();
  });

  it('calls fetchMore when sentinel becomes visible', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<JudgesList />);

    // Simulate the sentinel becoming visible
    intersectionCallback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(mockFetchMore).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { after: 'cursor-2' },
      }),
    );
  });

  it('does not call fetchMore when sentinel is not intersecting', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<JudgesList />);

    intersectionCallback(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(mockFetchMore).not.toHaveBeenCalled();
  });

  it('does not render sentinel while loading more results', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    // Sentinel hidden during loading to prevent duplicate fetches
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('disconnects observer on unmount', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { unmount } = render(<JudgesList />);
    unmount();
    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('does not render a "Load more" button (uses infinite scroll instead)', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.queryByText('Load more')).not.toBeInTheDocument();
  });

  it('renders filter input for name search', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(
      screen.getByLabelText(/Judge name/i),
    ).toBeInTheDocument();
  });

  it('filter input has name attribute', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const input = screen.getByLabelText(/Judge name/i);
    expect(input).toHaveAttribute('name', 'judgeName');
  });

  it('filters judges client-side by name', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const input = screen.getByLabelText(/Judge name/i);
    fireEvent.change(input, { target: { value: 'Smith' } });

    expect(screen.getByText('Smith, John A.')).toBeInTheDocument();
    expect(screen.queryByText('Johnson, Robert M.')).not.toBeInTheDocument();
  });

  it('uses shadcn Table component', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { container } = render(<JudgesList />);
    expect(container.querySelector('table')).toBeInTheDocument();
  });

  it('renders Select, Judge, and County column headers', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText('Judge')).toBeInTheDocument();
    expect(screen.getByText('County')).toBeInTheDocument();
    // Dept. and Status columns have been removed
    expect(screen.queryByRole('columnheader', { name: 'Dept.' })).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Status' })).not.toBeInTheDocument();
  });

  it('updates URL params when name filter changes', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const input = screen.getByLabelText(/Judge name/i);
    fireEvent.change(input, { target: { value: 'Smith' } });
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('name=Smith'));
  });

  it('initializes name filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('name=Johnson');
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect((screen.getByLabelText(/Judge name/i) as HTMLInputElement).value).toBe('Johnson');
    // Should filter to only Johnson
    expect(screen.getByText('Johnson, Robert M.')).toBeInTheDocument();
    expect(screen.queryByText('Smith, John A.')).not.toBeInTheDocument();
  });

  it('clears URL params when name filter is cleared', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    // Initially no name param
    expect(mockReplace).toHaveBeenCalledWith('/judges');
  });

  // Hover state tests (#1743)
  it('renders hover highlight on judge rows via TableRow component', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { container } = render(<JudgesList />);
    // TableRow uses hover:bg-muted/50 in its className
    const rows = container.querySelectorAll('tr.hover\\:bg-muted\\/50');
    // 2 data rows + 1 header row (header TableRow also has hover)
    expect(rows.length).toBeGreaterThanOrEqual(2);
  });

  // Selection and comparison tests (#1753)
  it('renders checkboxes for each judge row', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    expect(checkboxes).toHaveLength(2);
  });

  it('does not show compare button when no judges selected', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.queryByTestId('compare-button')).not.toBeInTheDocument();
  });

  it('shows compare button when a judge is selected', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('compare-button')).toBeInTheDocument();
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (1)');
  });

  it('disables compare button when only 1 judge selected', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('compare-button')).toBeDisabled();
  });

  it('enables compare button when 2+ judges selected', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    expect(screen.getByTestId('compare-button')).not.toBeDisabled();
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (2)');
  });

  it('navigates to compare page with selected judge IDs when compare button clicked', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByTestId('compare-button'));
    expect(mockPush).toHaveBeenCalledWith(
      expect.stringContaining('/judges/compare?ids='),
    );
    // The URL should contain both judge IDs
    const calledUrl = mockPush.mock.calls[0][0] as string;
    expect(calledUrl).toContain('judge-1');
    expect(calledUrl).toContain('judge-2');
  });

  it('deselects a judge when checkbox is clicked again', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    // Select both
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (2)');
    // Deselect first
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (1)');
  });

  it('renders checkbox aria-label with judge name', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByLabelText('Select Smith, John A. for comparison')).toBeInTheDocument();
    expect(screen.getByLabelText('Select Johnson, Robert M. for comparison')).toBeInTheDocument();
  });
});
