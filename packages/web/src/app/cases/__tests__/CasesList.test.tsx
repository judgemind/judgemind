import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Radix UI Select uses pointer events and pointer capture, which jsdom does not support.
// Stub the missing methods so Radix doesn't throw in tests.
beforeEach(() => {
  Element.prototype.hasPointerCapture = vi.fn().mockReturnValue(false);
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
  // Radix Select also uses scrollIntoView
  Element.prototype.scrollIntoView = vi.fn();
});

const mockUseQuery = vi.fn();
vi.mock('@apollo/client', () => ({
  useQuery: (...args: unknown[]) => mockUseQuery(...args),
  gql: (strings: TemplateStringsArray) => strings.join(''),
}));

const mockReplace = vi.fn();
let mockSearchParamsValue = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: mockReplace, back: vi.fn() }),
  useSearchParams: () => mockSearchParamsValue,
}));

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [key: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import { CasesList } from '../CasesList';

const MOCK_CASES_DATA = {
  cases: {
    edges: [
      { cursor: 'cursor-1', node: { id: 'case-1', caseNumber: '24STCV01234', caseTitle: 'Smith v. Jones', caseType: 'civil', caseStatus: 'active', court: { courtName: 'Superior Court of California', county: 'Los Angeles' } } },
      { cursor: 'cursor-2', node: { id: 'case-2', caseNumber: '24NNCV05678', caseTitle: null, caseType: null, caseStatus: 'closed', court: { courtName: 'Superior Court of California', county: 'San Bernardino' } } },
    ],
    pageInfo: { hasNextPage: true, endCursor: 'cursor-2' },
  },
};

describe('CasesList', () => {
  beforeEach(() => { vi.clearAllMocks(); mockSearchParamsValue = new URLSearchParams(); });

  it('renders skeleton rows while loading', () => {
    mockUseQuery.mockReturnValue({ data: undefined, loading: true, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
  });

  it('renders error state', () => {
    mockUseQuery.mockReturnValue({ data: undefined, loading: false, error: new Error('Network error'), fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText(/Failed to load cases/)).toBeInTheDocument();
  });

  it('renders empty state when no cases found', () => {
    mockUseQuery.mockReturnValue({ data: { cases: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } } }, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText(/No cases found/)).toBeInTheDocument();
  });

  it('renders case rows with case numbers and titles', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText('24STCV01234')).toBeInTheDocument();
    expect(screen.getByText('Smith v. Jones')).toBeInTheDocument();
    expect(screen.getByText('24NNCV05678')).toBeInTheDocument();
  });

  it('renders court info for each case', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText(/Los Angeles/)).toBeInTheDocument();
    expect(screen.getByText(/San Bernardino/)).toBeInTheDocument();
  });

  it('renders status badges for cases', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    // With shadcn Select, option text is rendered in a portal (not visible until opened),
    // so "Active" / "Closed" appear only in status badges
    expect(screen.getAllByText('Active').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Closed').length).toBeGreaterThanOrEqual(1);
  });

  it('renders case links pointing to detail pages', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText('24STCV01234').closest('a')).toHaveAttribute('href', '/cases/case-1');
  });

  it('renders Load more button when hasNextPage is true', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText('Load more')).toBeInTheDocument();
  });

  it('calls fetchMore when Load more is clicked', () => {
    const mockFetchMore = vi.fn();
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: mockFetchMore });
    render(<CasesList />);
    fireEvent.click(screen.getByText('Load more'));
    expect(mockFetchMore).toHaveBeenCalledWith(expect.objectContaining({ variables: { after: 'cursor-2' } }));
  });

  it('renders filter inputs including shadcn Select triggers for status and type', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByPlaceholderText(/Case number or title/i)).toBeInTheDocument();
    // shadcn Select renders as button triggers with aria-label
    expect(screen.getByLabelText(/Case status/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Case type/i)).toBeInTheDocument();
  });

  it('filter inputs have aria-label attributes', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const caseFilter = screen.getByLabelText('Case number or title');
    expect(caseFilter).toHaveAttribute('name', 'caseFilter');
    // shadcn Select triggers are buttons with aria-label
    const statusTrigger = screen.getByLabelText('Case status');
    expect(statusTrigger.tagName.toLowerCase()).toBe('button');
    const typeTrigger = screen.getByLabelText('Case type');
    expect(typeTrigger.tagName.toLowerCase()).toBe('button');
  });

  it('filters cases client-side by case number', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    fireEvent.change(screen.getByPlaceholderText(/Case number or title/i), { target: { value: '24STCV' } });
    expect(screen.getByText('24STCV01234')).toBeInTheDocument();
    expect(screen.queryByText('24NNCV05678')).not.toBeInTheDocument();
  });

  it('renders shadcn Select triggers showing default placeholder text', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    // Both triggers show "All statuses" / "All types" by default (since value is "all")
    expect(screen.getByText('All statuses')).toBeInTheDocument();
    expect(screen.getByText('All types')).toBeInTheDocument();
  });

  it('does not use raw select elements (uses shadcn Select)', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    // No native <select> elements should exist
    expect(container.querySelectorAll('select').length).toBe(0);
  });

  it('initializes case type filter from URL params and passes to query', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=probate');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    // Verify the query is called with the correct caseType from URL
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBe('probate');
    // Verify the Select trigger shows the selected value (Probate)
    expect(screen.getByText('Probate')).toBeInTheDocument();
  });

  it('initializes status filter from URL params and passes to query', () => {
    mockSearchParamsValue = new URLSearchParams('status=active');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.caseStatus).toBe('active');
  });

  it('updates URL when status filter is set via URL params', () => {
    mockSearchParamsValue = new URLSearchParams('status=dismissed');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('status=dismissed'));
  });

  it('updates URL when case type filter is set via URL params', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=family');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=family'));
  });

  it('does not pass caseType when "All types" is selected', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBeUndefined();
  });

  it('does not pass caseStatus when no status filter is set', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.caseStatus).toBeUndefined();
  });

  it('uses shadcn Table component structure', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    expect(container.querySelector('table')).toBeInTheDocument();
    expect(container.querySelector('thead')).toBeInTheDocument();
    expect(container.querySelector('tbody')).toBeInTheDocument();
  });

  it('uses shadcn Badge for status', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    expect(container.querySelectorAll('.rounded-full').length).toBeGreaterThan(0);
  });

  it('updates query variables when status filter is changed via user interaction', async () => {
    const user = userEvent.setup();
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);

    // Click the status select trigger to open the dropdown
    await user.click(screen.getByLabelText(/Case status/i));

    // Click on the 'Active' option
    const activeOption = await screen.findByRole('option', { name: 'Active' });
    await user.click(activeOption);

    // Assert query was re-run with new caseStatus variable
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseStatus).toBe('active');

    // Assert URL was updated
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('status=active'));
  });

  it('updates query variables when case type filter is changed via user interaction', async () => {
    const user = userEvent.setup();
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);

    // Click the case type select trigger to open the dropdown
    await user.click(screen.getByLabelText(/Case type/i));

    // Click on the 'Family' option
    const familyOption = await screen.findByRole('option', { name: 'Family' });
    await user.click(familyOption);

    // Assert query was re-run with new caseType variable
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseType).toBe('family');

    // Assert URL was updated
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=family'));
  });
});
