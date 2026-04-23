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

import JudgesLoading from '../loading';

describe('JudgesLoading', () => {
  it('renders without crashing', () => {
    const { container } = render(<JudgesLoading />);
    expect(container).toBeTruthy();
  });

  it('renders a skeleton table', () => {
    render(<JudgesLoading />);
    expect(screen.getByTestId('table')).toBeInTheDocument();
  });

  it('renders Name, County, and Rulings table headers', () => {
    render(<JudgesLoading />);
    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(screen.getByText('County')).toBeInTheDocument();
    expect(screen.getByText('Rulings')).toBeInTheDocument();
    const headers = screen.getAllByRole('columnheader');
    expect(headers).toHaveLength(3);
  });

  it('renders 10 skeleton rows plus 1 header row', () => {
    render(<JudgesLoading />);
    const rows = screen.getAllByTestId('table-row');
    // 1 header row + 10 skeleton rows = 11
    expect(rows).toHaveLength(11);
  });
});
