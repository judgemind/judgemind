import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MockedProvider, MockedResponse } from '@apollo/client/testing';
import { gql } from '@apollo/client';
import { SiblingRulings } from '../SiblingRulings';

// Mock next/link
vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

const SIBLING_RULINGS_QUERY = gql`
  query SiblingRulings($caseId: ID!, $first: Int!) {
    rulings(caseId: $caseId, first: $first) {
      edges {
        node {
          id
          hearingDate
          motionType
          outcome
        }
      }
    }
  }
`;

function buildMock(edges: Array<{ node: { id: string; hearingDate: string; motionType: string | null; outcome: string | null } }>): MockedResponse[] {
  return [
    {
      request: {
        query: SIBLING_RULINGS_QUERY,
        variables: { caseId: 'case-1', first: 100 },
      },
      result: {
        data: {
          rulings: { edges },
        },
      },
    },
  ];
}

describe('SiblingRulings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders sibling rulings when there are other rulings on the case', async () => {
    const mocks = buildMock([
      { node: { id: 'ruling-1', hearingDate: '2026-03-10', motionType: 'msj', outcome: 'granted' } },
      { node: { id: 'ruling-2', hearingDate: '2026-03-05', motionType: 'mtd', outcome: 'denied' } },
      { node: { id: 'ruling-3', hearingDate: '2026-02-20', motionType: 'demurrer', outcome: 'granted_in_part' } },
    ]);

    render(
      <MockedProvider mocks={mocks} addTypename={false}>
        <SiblingRulings caseId="case-1" currentRulingId="ruling-1" />
      </MockedProvider>,
    );

    // Wait for query to resolve
    const section = await screen.findByTestId('sibling-rulings-section');
    expect(section).toBeInTheDocument();

    // The heading should be visible
    expect(screen.getByText('Other Rulings on This Case')).toBeInTheDocument();

    // Current ruling (ruling-1) should be excluded
    const links = section.querySelectorAll('a');
    const hrefs = Array.from(links).map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/rulings/ruling-2');
    expect(hrefs).toContain('/rulings/ruling-3');
    expect(hrefs).not.toContain('/rulings/ruling-1');
  });

  it('does not render section when case has only the current ruling', async () => {
    const mocks = buildMock([
      { node: { id: 'ruling-1', hearingDate: '2026-03-10', motionType: 'msj', outcome: 'granted' } },
    ]);

    render(
      <MockedProvider mocks={mocks} addTypename={false}>
        <SiblingRulings caseId="case-1" currentRulingId="ruling-1" />
      </MockedProvider>,
    );

    // Wait for Apollo to resolve, then check that the section is not rendered
    // We need to wait a tick for the query to complete
    await vi.waitFor(() => {
      expect(screen.queryByTestId('sibling-rulings-loading')).not.toBeInTheDocument();
    });

    expect(screen.queryByTestId('sibling-rulings-section')).not.toBeInTheDocument();
    expect(screen.queryByText('Other Rulings on This Case')).not.toBeInTheDocument();
  });

  it('does not render section when case has no rulings', async () => {
    const mocks = buildMock([]);

    render(
      <MockedProvider mocks={mocks} addTypename={false}>
        <SiblingRulings caseId="case-1" currentRulingId="ruling-1" />
      </MockedProvider>,
    );

    await vi.waitFor(() => {
      expect(screen.queryByTestId('sibling-rulings-loading')).not.toBeInTheDocument();
    });

    expect(screen.queryByTestId('sibling-rulings-section')).not.toBeInTheDocument();
  });

  it('shows loading state initially', () => {
    const mocks = buildMock([
      { node: { id: 'ruling-2', hearingDate: '2026-03-05', motionType: 'mtd', outcome: 'denied' } },
    ]);

    render(
      <MockedProvider mocks={mocks} addTypename={false}>
        <SiblingRulings caseId="case-1" currentRulingId="ruling-1" />
      </MockedProvider>,
    );

    expect(screen.getByTestId('sibling-rulings-loading')).toBeInTheDocument();
  });

  it('renders each sibling as a link to its ruling detail page', async () => {
    const mocks = buildMock([
      { node: { id: 'ruling-1', hearingDate: '2026-03-10', motionType: 'msj', outcome: 'granted' } },
      { node: { id: 'ruling-2', hearingDate: '2026-03-05', motionType: 'mtd', outcome: 'denied' } },
    ]);

    render(
      <MockedProvider mocks={mocks} addTypename={false}>
        <SiblingRulings caseId="case-1" currentRulingId="ruling-1" />
      </MockedProvider>,
    );

    const section = await screen.findByTestId('sibling-rulings-section');
    const links = section.querySelectorAll('a');
    expect(links).toHaveLength(1); // Only ruling-2, ruling-1 is excluded
    expect(links[0]).toHaveAttribute('href', '/rulings/ruling-2');
  });

  it('displays motion type and outcome for each sibling', async () => {
    const mocks = buildMock([
      { node: { id: 'ruling-1', hearingDate: '2026-03-10', motionType: 'msj', outcome: 'granted' } },
      { node: { id: 'ruling-2', hearingDate: '2026-03-05', motionType: null, outcome: null } },
    ]);

    render(
      <MockedProvider mocks={mocks} addTypename={false}>
        <SiblingRulings caseId="case-1" currentRulingId="ruling-1" />
      </MockedProvider>,
    );

    await screen.findByTestId('sibling-rulings-section');

    // ruling-2 has null motionType so should show em-dash
    expect(screen.getByText('\u2014')).toBeInTheDocument();
  });

  it('does not render on GraphQL error', async () => {
    const mocks: MockedResponse[] = [
      {
        request: {
          query: SIBLING_RULINGS_QUERY,
          variables: { caseId: 'case-1', first: 100 },
        },
        error: new Error('Network error'),
      },
    ];

    render(
      <MockedProvider mocks={mocks} addTypename={false}>
        <SiblingRulings caseId="case-1" currentRulingId="ruling-1" />
      </MockedProvider>,
    );

    await vi.waitFor(() => {
      expect(screen.queryByTestId('sibling-rulings-loading')).not.toBeInTheDocument();
    });

    expect(screen.queryByTestId('sibling-rulings-section')).not.toBeInTheDocument();
  });
});
