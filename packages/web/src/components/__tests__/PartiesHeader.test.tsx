import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import { PartyList, PartiesSection } from '../PartiesHeader';

describe('PartyList', () => {
  it('renders the label heading', () => {
    render(<PartyList label="Plaintiffs" parties={[]} />);
    expect(screen.getByText('Plaintiffs')).toBeInTheDocument();
  });

  it('renders "None listed" when parties array is empty', () => {
    render(<PartyList label="Defendants" parties={[]} />);
    expect(screen.getByText('None listed')).toBeInTheDocument();
  });

  it('renders party names', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice Smith', partyType: null, role: 'plaintiff' },
      { id: '2', canonicalName: 'Bob Jones', partyType: null, role: 'plaintiff' },
    ];
    render(<PartyList label="Plaintiffs" parties={parties} />);
    expect(screen.getByText('Alice Smith')).toBeInTheDocument();
    expect(screen.getByText('Bob Jones')).toBeInTheDocument();
  });

  it('renders partyType in parentheses when present', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice Smith', partyType: 'individual', role: 'plaintiff' },
    ];
    render(<PartyList label="Plaintiffs" parties={parties} />);
    expect(screen.getByText('(Individual)')).toBeInTheDocument();
  });

  it('does not render partyType when null', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice Smith', partyType: null, role: 'plaintiff' },
    ];
    const { container } = render(<PartyList label="Plaintiffs" parties={parties} />);
    expect(container.textContent).not.toContain('(');
  });
});

describe('PartiesSection', () => {
  it('renders grouped parties', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice Smith', partyType: null, role: 'plaintiff' },
      { id: '2', canonicalName: 'Bob Jones', partyType: null, role: 'defendant' },
    ];
    render(<PartiesSection parties={parties} />);
    expect(screen.getByText('Plaintiffs')).toBeInTheDocument();
    expect(screen.getByText('Defendants')).toBeInTheDocument();
    expect(screen.getByText('Alice Smith')).toBeInTheDocument();
    expect(screen.getByText('Bob Jones')).toBeInTheDocument();
  });

  it('renders the parties-section test id', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
    ];
    render(<PartiesSection parties={parties} />);
    expect(screen.getByTestId('parties-section')).toBeInTheDocument();
  });

  it('renders filedAt when provided', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
    ];
    render(<PartiesSection parties={parties} filedAt="2024-03-15" />);
    expect(screen.getByText('Filed Date')).toBeInTheDocument();
    expect(screen.getByText('Mar 15, 2024')).toBeInTheDocument();
  });

  it('does not render filedAt section when not provided', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
    ];
    render(<PartiesSection parties={parties} />);
    expect(screen.queryByText('Filed Date')).not.toBeInTheDocument();
  });

  it('renders only filedAt when parties is empty', () => {
    render(<PartiesSection parties={[]} filedAt="2024-01-01" />);
    expect(screen.getByText('Filed Date')).toBeInTheDocument();
    // Should not render party group headings when there are no parties
    expect(screen.queryByText('Plaintiffs')).not.toBeInTheDocument();
  });

  it('returns null when no parties and no filedAt', () => {
    const { container } = render(<PartiesSection parties={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders Other Parties section when others exist', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
      { id: '2', canonicalName: 'Charlie', partyType: null, role: 'intervenor' },
    ];
    render(<PartiesSection parties={parties} />);
    expect(screen.getByText('Other Parties')).toBeInTheDocument();
    expect(screen.getByText('Charlie')).toBeInTheDocument();
  });

  it('does not render Other Parties section when there are none', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
      { id: '2', canonicalName: 'Bob', partyType: null, role: 'defendant' },
    ];
    render(<PartiesSection parties={parties} />);
    expect(screen.queryByText('Other Parties')).not.toBeInTheDocument();
  });
});
