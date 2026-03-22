import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: ({ className }: { className?: string }) => (
    <div data-testid="skeleton" className={className} />
  ),
}));

vi.mock('@/components/ui/table', () => ({
  Table: ({ children }: { children: React.ReactNode }) => (
    <table data-testid="table">{children}</table>
  ),
  TableHeader: ({ children }: { children: React.ReactNode }) => (
    <thead>{children}</thead>
  ),
  TableBody: ({ children }: { children: React.ReactNode }) => (
    <tbody data-testid="table-body">{children}</tbody>
  ),
  TableRow: ({ children }: { children: React.ReactNode }) => (
    <tr data-testid="table-row">{children}</tr>
  ),
  TableHead: ({ children }: { children: React.ReactNode }) => (
    <th>{children}</th>
  ),
  TableCell: ({ children }: { children: React.ReactNode }) => (
    <td>{children}</td>
  ),
}));

import CasesLoading from '../loading';

describe('CasesLoading', () => {
  it('renders without crashing', () => {
    const { container } = render(<CasesLoading />);
    expect(container).toBeTruthy();
  });

  it('renders a skeleton table', () => {
    render(<CasesLoading />);
    expect(screen.getByTestId('table')).toBeInTheDocument();
  });

  it('renders table headers', () => {
    render(<CasesLoading />);
    expect(screen.getByText('Case')).toBeInTheDocument();
    expect(screen.getByText('Court')).toBeInTheDocument();
    expect(screen.getByText('Type')).toBeInTheDocument();
    expect(screen.getByText('Status')).toBeInTheDocument();
  });

  it('renders 8 skeleton rows plus 1 header row', () => {
    render(<CasesLoading />);
    const rows = screen.getAllByTestId('table-row');
    // 1 header row + 8 skeleton rows = 9
    expect(rows).toHaveLength(9);
  });
});
