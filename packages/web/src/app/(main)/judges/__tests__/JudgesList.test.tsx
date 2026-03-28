import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Mock Apollo client — track calls by query name
const mockUseQuery = vi.fn();
vi.mock('@apollo/client', () => ({
  useQuery: (...args: unknown[]) => mockUseQuery(...args),
  gql: (strings: TemplateStringsArray) => strings.join(''),
}));

const mockPush = vi.fn();
const mockReplace = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace, back: vi.fn() }),
  useSearchParams: () => mockSearchParams,
}));

// Mock useCountyOptions from filter-options
vi.mock('@/lib/filter-options', () => ({
  useCountyOptions: () => ['Los Angeles', 'Orange', 'San Diego'],
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
  ArrowUpDown: ({ className }: { className?: string }) => (
    <span data-testid="arrow-updown-icon" className={className} />
  ),
  ArrowUp: ({ className }: { className?: string }) => (
    <span data-testid="arrow-up-icon" className={className} />
  ),
  ArrowDown: ({ className }: { className?: string }) => (
    <span data-testid="arrow-down-icon" className={className} />
  ),
  X: ({ className }: { className?: string }) => (
    <span data-testid="x-icon" className={className} />
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

// Mock Select component
vi.mock('@/components/ui/select', () => ({
  Select: ({ children, onValueChange, value }: { children: React.ReactNode; onValueChange: (v: string) => void; value: string }) => (
    <div data-testid="county-select" data-value={value}>
      {children}
      <select
        data-testid="county-select-native"
        value={value}
        onChange={(e) => onValueChange(e.target.value)}
        aria-hidden="true"
        style={{ display: 'none' }}
      >
        <option value="all">All counties</option>
        <option value="Los Angeles">Los Angeles</option>
        <option value="Orange">Orange</option>
      </select>
    </div>
  ),
  SelectTrigger: ({ children, ...props }: { children: React.ReactNode; [key: string]: unknown }) => (
    <button data-testid="county-select-trigger" {...props}>{children}</button>
  ),
  SelectValue: ({ placeholder }: { placeholder?: string }) => (
    <span>{placeholder}</span>
  ),
  SelectContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children, value }: { children: React.ReactNode; value: string }) => (
    <div data-value={value}>{children}</div>
  ),
}));

import { JudgesList } from '../JudgesList';

const MOCK_JUDGES_DATA = {
  judges: {
    edges: [
      {
        cursor: 'cursor-1',
        node: {
          id: 'judge-1',
          canonicalName: 'John A. Smith',
          department: '42',
          isActive: true,
          rulingCount: 55,
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
          canonicalName: 'Robert M. Johnson',
          department: null,
          isActive: false,
          rulingCount: 23,
          court: {
            courtName: 'Superior Court of California',
            county: 'Orange',
          },
        },
      },
      {
        cursor: 'cursor-3',
        node: {
          id: 'judge-3',
          canonicalName: 'Alice Z. Anderson',
          department: null,
          isActive: true,
          rulingCount: 100,
          court: {
            courtName: 'Superior Court of California',
            county: 'Los Angeles',
          },
        },
      },
    ],
    pageInfo: {
      hasNextPage: false,
      endCursor: 'cursor-3',
    },
  },
};

function setupMocks({
  judgesData = MOCK_JUDGES_DATA,
  loading = false,
  error,
}: {
  judgesData?: typeof MOCK_JUDGES_DATA | { judges: { edges: []; pageInfo: { hasNextPage: false; endCursor: null } } };
  loading?: boolean;
  error?: Error;
} = {}) {
  // useQuery is called with different queries — match on the query string content
  // Counties are now provided by useCountyOptions (mocked above), not via useQuery
  mockUseQuery.mockImplementation((query: string) => {
    if (typeof query === 'string' && query.includes('JudgesPage2')) {
      return { data: undefined };
    }
    // Default: JUDGES_QUERY
    return { data: loading ? undefined : judgesData, loading, error };
  });
}

describe('JudgesList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParams = new URLSearchParams();
  });

  it('renders skeleton rows while loading', () => {
    setupMocks({ loading: true });

    const { container } = render(<JudgesList />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders error state with soft styling', () => {
    setupMocks({ error: new Error('Network error') });

    const { container } = render(<JudgesList />);
    expect(screen.getByText(/Failed to load judges/)).toBeInTheDocument();
    expect(container.querySelector('.text-destructive')).not.toBeInTheDocument();
    expect(container.querySelector('.text-red-700')).toBeInTheDocument();
    expect(screen.getByTestId('alert-circle-icon')).toBeInTheDocument();
    expect(screen.getByText('Try again')).toBeInTheDocument();
  });

  it('renders empty state when no judges found', () => {
    setupMocks({
      judgesData: {
        judges: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } },
      },
    });

    render(<JudgesList />);
    expect(screen.getByText(/No judges found/)).toBeInTheDocument();
  });

  it('renders judge names sorted by surname by default', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByText('Alice Z. Anderson')).toBeInTheDocument();
    expect(screen.getByText('Robert M. Johnson')).toBeInTheDocument();
    expect(screen.getByText('John A. Smith')).toBeInTheDocument();

    // Check order: Anderson < Johnson < Smith (by surname)
    const links = screen.getAllByRole('link');
    const names = links.map((l) => l.textContent).filter(Boolean);
    expect(names).toEqual(['Alice Z. Anderson', 'Robert M. Johnson', 'John A. Smith']);
  });

  it('renders court county for each judge', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getAllByText(/Los Angeles/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Orange/).length).toBeGreaterThan(0);
  });

  it('renders ruling count for each judge', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByText('55')).toBeInTheDocument();
    expect(screen.getByText('23')).toBeInTheDocument();
    expect(screen.getByText('100')).toBeInTheDocument();
  });

  it('renders department inline with judge name when available', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByText(/Dept\. 42/)).toBeInTheDocument();
    const judgeLink = screen.getByText('John A. Smith');
    const cell = judgeLink.closest('td');
    expect(cell?.textContent).toContain('Dept. 42');
  });

  it('does not render department text when department is null', () => {
    setupMocks();

    render(<JudgesList />);
    const johnsonLink = screen.getByText('Robert M. Johnson');
    const cell = johnsonLink.closest('td');
    expect(cell?.textContent).not.toContain('Dept.');
  });

  it('renders judge links pointing to detail pages', () => {
    setupMocks();

    render(<JudgesList />);
    const link = screen.getByText('John A. Smith').closest('a');
    expect(link).toHaveAttribute('href', '/judges/judge-1');
  });

  it('navigates to judge detail when clicking anywhere on the row', () => {
    setupMocks();

    render(<JudgesList />);
    // Click on the ruling count cell (not the name link) — should still navigate
    // "100" belongs to Anderson (judge-3), sorted first by surname
    fireEvent.click(screen.getByText('100'));
    expect(mockPush).toHaveBeenCalledWith('/judges/judge-3');
  });

  it('does not navigate when clicking the checkbox', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    // Checkbox click should toggle selection, not navigate
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('does not double-navigate when clicking the name link', () => {
    setupMocks();

    render(<JudgesList />);
    const link = screen.getByText('Alice Z. Anderson');
    fireEvent.click(link);
    // Link stopPropagation prevents row onClick from firing router.push
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('renders sortable column headers', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByRole('button', { name: /Name/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /County/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Rulings/ })).toBeInTheDocument();
  });

  it('sorts by ruling count descending when column clicked', () => {
    setupMocks();

    render(<JudgesList />);
    fireEvent.click(screen.getByRole('button', { name: /Rulings/ }));

    const links = screen.getAllByRole('link');
    const names = links.map((l) => l.textContent).filter(Boolean);
    // Descending by ruling count: Anderson (100) > Smith (55) > Johnson (23)
    expect(names).toEqual(['Alice Z. Anderson', 'John A. Smith', 'Robert M. Johnson']);
  });

  it('toggles sort direction when same column clicked twice', () => {
    setupMocks();

    render(<JudgesList />);
    // Default sort is name 'asc'. First click toggles to 'desc'.
    fireEvent.click(screen.getByRole('button', { name: /Name/ }));

    let links = screen.getAllByRole('link');
    let names = links.map((l) => l.textContent).filter(Boolean);
    // Descending by surname: Smith > Johnson > Anderson
    expect(names).toEqual(['John A. Smith', 'Robert M. Johnson', 'Alice Z. Anderson']);

    // Second click toggles back to 'asc'.
    fireEvent.click(screen.getByRole('button', { name: /Name/ }));
    links = screen.getAllByRole('link');
    names = links.map((l) => l.textContent).filter(Boolean);
    // Ascending by surname: Anderson < Johnson < Smith
    expect(names).toEqual(['Alice Z. Anderson', 'Robert M. Johnson', 'John A. Smith']);
  });

  it('sorts by county when County column clicked', () => {
    setupMocks();

    render(<JudgesList />);
    fireEvent.click(screen.getByRole('button', { name: /County/ }));

    const links = screen.getAllByRole('link');
    const names = links.map((l) => l.textContent).filter(Boolean);
    // Ascending by county: LA (Anderson, Smith) < Orange (Johnson)
    // Within LA, sorted by surname: Anderson < Smith
    expect(names).toEqual(['Alice Z. Anderson', 'John A. Smith', 'Robert M. Johnson']);
  });

  it('filters judges client-side by name', () => {
    setupMocks();

    render(<JudgesList />);
    const input = screen.getByLabelText(/Judge name/i);
    fireEvent.change(input, { target: { value: 'Smith' } });

    expect(screen.getByText('John A. Smith')).toBeInTheDocument();
    expect(screen.queryByText('Robert M. Johnson')).not.toBeInTheDocument();
    expect(screen.queryByText('Alice Z. Anderson')).not.toBeInTheDocument();
  });

  it('renders filter input', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByLabelText(/Judge name/i)).toBeInTheDocument();
  });

  it('filter input has name attribute', () => {
    setupMocks();

    render(<JudgesList />);
    const input = screen.getByLabelText(/Judge name/i);
    expect(input).toHaveAttribute('name', 'judgeName');
  });

  it('uses shadcn Table component', () => {
    setupMocks();

    const { container } = render(<JudgesList />);
    expect(container.querySelector('table')).toBeInTheDocument();
  });

  it('displays judge count at the bottom', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByText('3 judges')).toBeInTheDocument();
  });

  // Selection and comparison tests
  it('renders checkboxes for each judge row', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    expect(checkboxes).toHaveLength(3);
  });

  it('does not show compare button when no judges selected', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.queryByTestId('compare-button')).not.toBeInTheDocument();
  });

  it('shows compare button when a judge is selected', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('compare-button')).toBeInTheDocument();
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (1)');
  });

  it('disables compare button when only 1 judge selected', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('compare-button')).toBeDisabled();
  });

  it('enables compare button when 2+ judges selected', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    expect(screen.getByTestId('compare-button')).not.toBeDisabled();
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (2)');
  });

  it('navigates to compare page with selected judge IDs', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByTestId('compare-button'));
    expect(mockPush).toHaveBeenCalledWith(
      expect.stringContaining('/judges/compare?ids='),
    );
  });

  it('deselects a judge when checkbox is clicked again', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (2)');
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('compare-button')).toHaveTextContent('Compare (1)');
  });

  it('renders checkbox aria-label with judge name', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByLabelText('Select Alice Z. Anderson for comparison')).toBeInTheDocument();
    expect(screen.getByLabelText('Select John A. Smith for comparison')).toBeInTheDocument();
  });

  it('does not render a "Load more" button', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.queryByText('Load more')).not.toBeInTheDocument();
  });

  it('renders county filter select', () => {
    setupMocks();

    render(<JudgesList />);
    expect(screen.getByTestId('county-select')).toBeInTheDocument();
  });

  // Selection chips tests
  it('shows selection chips with surname when judges are selected', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]); // Anderson (first by surname sort)
    fireEvent.click(checkboxes[2]); // Smith

    expect(screen.getByTestId('selection-tray')).toBeInTheDocument();
    const chips = screen.getAllByTestId('selection-chip');
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent('Anderson');
    expect(chips[1]).toHaveTextContent('Smith');
  });

  it('removes a chip when its dismiss button is clicked', () => {
    setupMocks();

    render(<JudgesList />);
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]); // Anderson
    fireEvent.click(checkboxes[2]); // Smith

    const removeButtons = screen.getAllByLabelText(/Remove .* from comparison/);
    fireEvent.click(removeButtons[0]); // remove Anderson

    const chips = screen.getAllByTestId('selection-chip');
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent('Smith');
  });

  it('keeps chips visible when judges are filtered out', () => {
    setupMocks();

    render(<JudgesList />);
    // Select Anderson (LA) and Johnson (Orange)
    const checkboxes = screen.getAllByTestId('judge-checkbox');
    fireEvent.click(checkboxes[0]); // Anderson
    fireEvent.click(checkboxes[1]); // Johnson

    // Filter to only show Smith — Anderson and Johnson disappear from table
    const input = screen.getByLabelText(/Judge name/i);
    fireEvent.change(input, { target: { value: 'Smith' } });

    // Chips should still be visible even though those judges are filtered out
    const chips = screen.getAllByTestId('selection-chip');
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent('Anderson');
    expect(chips[1]).toHaveTextContent('Johnson');
  });

  // URL param sync tests
  describe('URL param sync', () => {
    it('initializes name filter from URL search params', () => {
      mockSearchParams = new URLSearchParams('name=Smith');
      setupMocks();

      render(<JudgesList />);
      const input = screen.getByLabelText(/Judge name/i);
      expect(input).toHaveValue('Smith');
      // Only Smith should be visible
      expect(screen.getByText('John A. Smith')).toBeInTheDocument();
      expect(screen.queryByText('Robert M. Johnson')).not.toBeInTheDocument();
    });

    it('initializes county filter from URL search params', () => {
      mockSearchParams = new URLSearchParams('county=Orange');
      setupMocks();

      render(<JudgesList />);
      const select = screen.getByTestId('county-select');
      expect(select).toHaveAttribute('data-value', 'Orange');
    });

    it('initializes sort key and direction from URL search params', () => {
      mockSearchParams = new URLSearchParams('sort=rulingCount&dir=desc');
      setupMocks();

      render(<JudgesList />);
      // With rulingCount desc: Anderson (100) > Smith (55) > Johnson (23)
      const links = screen.getAllByRole('link');
      const names = links.map((l) => l.textContent).filter(Boolean);
      expect(names).toEqual(['Alice Z. Anderson', 'John A. Smith', 'Robert M. Johnson']);
    });

    it('updates URL when name filter changes', () => {
      setupMocks();

      render(<JudgesList />);
      const input = screen.getByLabelText(/Judge name/i);
      fireEvent.change(input, { target: { value: 'Anderson' } });

      expect(mockReplace).toHaveBeenCalledWith(
        expect.stringContaining('name=Anderson'),
      );
    });

    it('updates URL when sort changes', () => {
      setupMocks();

      render(<JudgesList />);
      fireEvent.click(screen.getByRole('button', { name: /Rulings/ }));

      // rulingCount defaults to desc, so URL should include sort=rulingCount&dir=desc
      expect(mockReplace).toHaveBeenCalledWith(
        expect.stringContaining('sort=rulingCount'),
      );
      expect(mockReplace).toHaveBeenCalledWith(
        expect.stringContaining('dir=desc'),
      );
    });

    it('uses clean URL /judges when all filters are defaults', () => {
      setupMocks();

      render(<JudgesList />);
      // Default state: no name, county=all, sort=name, dir=asc — should be clean URL
      expect(mockReplace).toHaveBeenCalledWith('/judges');
    });

    it('combines multiple filters in URL', () => {
      mockSearchParams = new URLSearchParams('name=Smith&sort=rulingCount&dir=desc');
      setupMocks();

      render(<JudgesList />);
      // Should have both name and sort params in URL
      expect(mockReplace).toHaveBeenCalledWith(
        expect.stringMatching(/name=Smith.*sort=rulingCount|sort=rulingCount.*name=Smith/),
      );
    });
  });

  // County filter behavior tests
  describe('county filter', () => {
    it('passes county to GraphQL query when county filter is selected', () => {
      mockSearchParams = new URLSearchParams('county=Orange');
      setupMocks();

      render(<JudgesList />);
      // The JUDGES_QUERY should be called with county: 'Orange'
      const judgesCall = mockUseQuery.mock.calls.find(
        (call: unknown[]) => typeof call[0] === 'string' && call[0].includes('Judges') && !call[0].includes('JudgesPage2'),
      );
      expect(judgesCall).toBeDefined();
      expect(judgesCall![1]).toMatchObject({
        variables: expect.objectContaining({ county: 'Orange' }),
      });
    });

    it('passes undefined county when "all" is selected', () => {
      setupMocks();

      render(<JudgesList />);
      const judgesCall = mockUseQuery.mock.calls.find(
        (call: unknown[]) => typeof call[0] === 'string' && call[0].includes('Judges') && !call[0].includes('JudgesPage2'),
      );
      expect(judgesCall).toBeDefined();
      expect(judgesCall![1]).toMatchObject({
        variables: expect.objectContaining({ county: undefined }),
      });
    });
  });
});
