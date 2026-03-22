import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

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
    expect(screen.getAllByText('Active').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Closed').length).toBeGreaterThanOrEqual(2);
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

  it('renders filter inputs including case type dropdown', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByPlaceholderText(/Case number or title/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Case status/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Case type/i)).toBeInTheDocument();
  });

  it('filter inputs have name and aria-label attributes', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const caseFilter = screen.getByLabelText('Case number or title');
    expect(caseFilter).toHaveAttribute('name', 'caseFilter');
    const statusSelect = screen.getByLabelText('Case status');
    expect(statusSelect).toHaveAttribute('name', 'caseStatus');
    const typeSelect = screen.getByLabelText('Case type');
    expect(typeSelect).toHaveAttribute('name', 'caseType');
  });

  it('filters cases client-side by case number', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    fireEvent.change(screen.getByPlaceholderText(/Case number or title/i), { target: { value: '24STCV' } });
    expect(screen.getByText('24STCV01234')).toBeInTheDocument();
    expect(screen.queryByText('24NNCV05678')).not.toBeInTheDocument();
  });

  it('renders all case type options in the dropdown', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getAllByText('Civil').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Family')).toBeInTheDocument();
    expect(screen.getByText('Probate')).toBeInTheDocument();
    expect(screen.getByText('Small Claims')).toBeInTheDocument();
    expect(screen.getAllByText('Other').length).toBeGreaterThanOrEqual(1);
  });

  it('passes caseType to the GraphQL query when type filter is selected', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    fireEvent.change(screen.getByLabelText(/Case type/i), { target: { value: 'civil' } });
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseType).toBe('civil');
  });

  it('updates URL params when case type filter changes', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    fireEvent.change(screen.getByLabelText(/Case type/i), { target: { value: 'family' } });
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=family'));
  });

  it('initializes case type filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=probate');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect((screen.getByLabelText(/Case type/i) as HTMLSelectElement).value).toBe('probate');
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBe('probate');
  });

  it('does not pass caseType when "All types" is selected', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBeUndefined();
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
});
