import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { OverviewGrid } from '../src/app/admin/data-quality/OverviewGrid';
import type { DataQualityOverviewItem } from '../src/lib/data-quality-queries';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeItems(): DataQualityOverviewItem[] {
  return [
    {
      county: 'Los Angeles',
      healthStatus: 'green',
      rulingCount24h: 42,
      fieldCompletenessPct: 95.5,
      scraperLastSuccessAgeHours: 1.2,
      lastUpdated: '2026-03-11T10:00:00Z',
    },
    {
      county: 'Orange',
      healthStatus: 'yellow',
      rulingCount24h: 10,
      fieldCompletenessPct: 82.0,
      scraperLastSuccessAgeHours: 8.5,
      lastUpdated: '2026-03-11T09:00:00Z',
    },
    {
      county: 'San Diego',
      healthStatus: 'red',
      rulingCount24h: 0,
      fieldCompletenessPct: 45.0,
      scraperLastSuccessAgeHours: 30.0,
      lastUpdated: '2026-03-10T12:00:00Z',
    },
  ];
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('OverviewGrid', () => {
  let onCountyClick: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onCountyClick = vi.fn();
  });

  it('renders all county names', () => {
    render(
      <OverviewGrid items={makeItems()} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    expect(screen.getByText('Los Angeles')).toBeInTheDocument();
    expect(screen.getByText('Orange')).toBeInTheDocument();
    expect(screen.getByText('San Diego')).toBeInTheDocument();
  });

  it('renders health score as percentage', () => {
    render(
      <OverviewGrid items={makeItems()} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    // 1 of 3 green = 33%
    expect(screen.getByText('33%')).toBeInTheDocument();
  });

  it('renders active alerts count', () => {
    render(
      <OverviewGrid items={makeItems()} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText(/counties in red status/)).toBeInTheDocument();
  });

  it('renders rulings count per county', () => {
    render(
      <OverviewGrid items={makeItems()} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    expect(screen.getByText('42 rulings/24h')).toBeInTheDocument();
    expect(screen.getByText('10 rulings/24h')).toBeInTheDocument();
    expect(screen.getByText('0 rulings/24h')).toBeInTheDocument();
  });

  it('renders completeness percentages', () => {
    render(
      <OverviewGrid items={makeItems()} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    expect(screen.getByText('96% complete')).toBeInTheDocument();
    expect(screen.getByText('82% complete')).toBeInTheDocument();
    expect(screen.getByText('45% complete')).toBeInTheDocument();
  });

  it('calls onCountyClick when a county is clicked', () => {
    render(
      <OverviewGrid items={makeItems()} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    fireEvent.click(screen.getByText('Orange'));
    expect(onCountyClick).toHaveBeenCalledWith('Orange');
  });

  it('renders zero health score when no items', () => {
    render(
      <OverviewGrid items={[]} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    expect(screen.getByText('0%')).toBeInTheDocument();
    // Both alerts and total counties show 0
    const zeros = screen.getAllByText('0');
    expect(zeros.length).toBe(2);
  });

  it('hides metrics when values are null', () => {
    const items: DataQualityOverviewItem[] = [
      {
        county: 'Riverside',
        healthStatus: 'red',
        rulingCount24h: null,
        fieldCompletenessPct: null,
        scraperLastSuccessAgeHours: null,
        lastUpdated: null,
      },
    ];
    render(
      <OverviewGrid items={items} onCountyClick={onCountyClick} selectedCounty={null} />,
    );
    expect(screen.getByText('Riverside')).toBeInTheDocument();
    expect(screen.queryByText(/rulings/)).not.toBeInTheDocument();
    expect(screen.queryByText(/complete/)).not.toBeInTheDocument();
  });
});
