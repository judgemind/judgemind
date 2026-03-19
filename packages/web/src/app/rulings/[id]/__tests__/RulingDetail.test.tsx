import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RulingDetail } from '../RulingDetail';

// ---------------------------------------------------------------------------
// Mock next/link — render as an anchor tag
// ---------------------------------------------------------------------------

vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

// ---------------------------------------------------------------------------
// Test data factories
// ---------------------------------------------------------------------------

function buildFullRuling() {
  return {
    id: 'ruling-1',
    hearingDate: '2026-03-10',
    outcome: 'granted' as string | null,
    motionType: 'msj' as string | null,
    isTentative: true,
    department: '12' as string | null,
    rulingText: 'The motion is granted.\n\nThe court finds in favor of plaintiff.' as string | null,
    rulingTextHtml: null as string | null,
    summary: 'Court grants MSJ in favor of plaintiff.' as string | null,
    postedAt: '2026-03-09T18:00:00Z' as string | null,
    documentId: 'doc-1' as string | null,
    documentFormat: 'pdf' as string | null,
    case: {
      id: 'case-1',
      caseNumber: '25STCV12345',
      caseTitle: 'Smith v. Jones' as string | null,
    } as { id: string; caseNumber: string; caseTitle: string | null } | null,
    judge: {
      id: 'judge-1',
      canonicalName: 'Johnson, Robert M.',
    } as { id: string; canonicalName: string } | null,
    court: {
      courtName: 'Los Angeles Superior Court',
      county: 'Los Angeles',
    } as { courtName: string; county: string } | null,
  };
}

function buildMinimalRuling() {
  return {
    id: 'ruling-min',
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
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RulingDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // -------------------------------------------------------------------------
  // Full data rendering
  // -------------------------------------------------------------------------

  it('renders all sections when ruling has complete data', () => {
    const ruling = buildFullRuling();
    render(<RulingDetail ruling={ruling} />);

    // Case section
    expect(screen.getByText('Case')).toBeInTheDocument();
    expect(screen.getByText(/25STCV12345/)).toBeInTheDocument();
    expect(screen.getByText(/Smith v. Jones/)).toBeInTheDocument();

    // Judge section
    expect(screen.getByText('Judge')).toBeInTheDocument();
    expect(screen.getByText('Johnson, Robert M.')).toBeInTheDocument();

    // Summary section
    expect(screen.getByText('Summary')).toBeInTheDocument();
    expect(screen.getByText('Court grants MSJ in favor of plaintiff.')).toBeInTheDocument();

    // Ruling text section
    expect(screen.getByText('Ruling Text')).toBeInTheDocument();

    // Document download section
    expect(screen.getByText('Download original document')).toBeInTheDocument();
    expect(screen.getByText('PDF')).toBeInTheDocument();
  });

  it('renders case link with correct href', () => {
    const ruling = buildFullRuling();
    render(<RulingDetail ruling={ruling} />);

    const caseLink = screen.getByText(/25STCV12345/).closest('a');
    expect(caseLink).toHaveAttribute('href', '/cases/case-1');
  });

  it('renders judge link with correct href', () => {
    const ruling = buildFullRuling();
    render(<RulingDetail ruling={ruling} />);

    const judgeLink = screen.getByText('Johnson, Robert M.').closest('a');
    expect(judgeLink).toHaveAttribute('href', '/judges/judge-1');
  });

  it('renders download link with correct href from buildDownloadUrl', () => {
    const ruling = buildFullRuling();
    render(<RulingDetail ruling={ruling} />);

    const downloadLink = screen.getByText('Download original document').closest('a');
    expect(downloadLink).toHaveAttribute('href', expect.stringContaining('/api/documents/doc-1/download'));
    expect(downloadLink).toHaveAttribute('target', '_blank');
    expect(downloadLink).toHaveAttribute('rel', 'noopener noreferrer');
  });

  // -------------------------------------------------------------------------
  // Partial data — missing optional sections
  // -------------------------------------------------------------------------

  it('does not render case section when case is null', () => {
    const ruling = buildFullRuling();
    ruling.case = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Case')).not.toBeInTheDocument();
  });

  it('does not render judge section when judge is null', () => {
    const ruling = buildFullRuling();
    ruling.judge = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Judge')).not.toBeInTheDocument();
  });

  it('does not render summary section when summary is null', () => {
    const ruling = buildFullRuling();
    ruling.summary = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Summary')).not.toBeInTheDocument();
  });

  it('does not render ruling text section when rulingText is null', () => {
    const ruling = buildFullRuling();
    ruling.rulingText = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Ruling Text')).not.toBeInTheDocument();
  });

  it('does not render download section when documentId is null', () => {
    const ruling = buildFullRuling();
    ruling.documentId = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Download original document')).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Minimal data — all nullable fields null
  // -------------------------------------------------------------------------

  it('renders only the wrapper div when all nullable fields are null', () => {
    const ruling = buildMinimalRuling();
    const { container } = render(<RulingDetail ruling={ruling} />);

    // No section headings should be rendered
    expect(screen.queryByText('Case')).not.toBeInTheDocument();
    expect(screen.queryByText('Judge')).not.toBeInTheDocument();
    expect(screen.queryByText('Summary')).not.toBeInTheDocument();
    expect(screen.queryByText('Ruling Text')).not.toBeInTheDocument();
    expect(screen.queryByText('Download original document')).not.toBeInTheDocument();

    // The wrapper div should still exist
    expect(container.firstChild).toBeTruthy();
    expect(container.firstChild?.nodeName).toBe('DIV');
  });

  // -------------------------------------------------------------------------
  // Edge cases
  // -------------------------------------------------------------------------

  it('renders case number without title when caseTitle is null', () => {
    const ruling = buildFullRuling();
    ruling.case = { id: 'case-2', caseNumber: '25STCV99999', caseTitle: null };
    render(<RulingDetail ruling={ruling} />);

    const caseLink = screen.getByText('25STCV99999');
    expect(caseLink).toBeInTheDocument();
    // Should not contain an em-dash separator since there is no title
    expect(caseLink.textContent).toBe('25STCV99999');
  });

  it('renders download link without format badge when documentFormat is null', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.getByText('Download original document')).toBeInTheDocument();
    // No format badge should be rendered
    expect(screen.queryByText('PDF')).not.toBeInTheDocument();
    expect(screen.queryByText('HTML')).not.toBeInTheDocument();
  });

  it('renders uppercased format for unknown document formats', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'xlsx';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.getByText('XLSX')).toBeInTheDocument();
  });

  it('renders known FORMAT_LABELS for standard formats', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.getByText('HTML')).toBeInTheDocument();
  });

  it('splits ruling text into paragraphs via cleanRulingText', () => {
    const ruling = buildFullRuling();
    ruling.rulingText = 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.getByText('First paragraph.')).toBeInTheDocument();
    expect(screen.getByText('Second paragraph.')).toBeInTheDocument();
    expect(screen.getByText('Third paragraph.')).toBeInTheDocument();
  });

  it('renders case number with em-dash and title when caseTitle is present', () => {
    const ruling = buildFullRuling();
    render(<RulingDetail ruling={ruling} />);

    // The component concatenates: caseNumber + " — " + caseTitle
    const caseLink = screen.getByText(/25STCV12345/).closest('a');
    expect(caseLink?.textContent).toContain('\u2014');
    expect(caseLink?.textContent).toContain('Smith v. Jones');
  });

  // -------------------------------------------------------------------------
  // Formatted HTML rendering (rulingTextHtml)
  // -------------------------------------------------------------------------

  it('renders formatted HTML when rulingTextHtml is present', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<div class="ruling"><section class="ruling-body"><h3>Motion to Compel</h3><p>The motion is <strong>GRANTED</strong>.</p></section></div>';
    const { container } = render(<RulingDetail ruling={ruling} />);

    // Should render the formatted HTML
    expect(screen.getByText('Ruling Text')).toBeInTheDocument();
    const formattedDiv = container.querySelector('.ruling-formatted');
    expect(formattedDiv).toBeInTheDocument();
    expect(formattedDiv?.innerHTML).toContain('Motion to Compel');
    expect(formattedDiv?.innerHTML).toContain('<strong>GRANTED</strong>');
  });

  it('prefers rulingTextHtml over rulingText when both are present', () => {
    const ruling = buildFullRuling();
    ruling.rulingText = 'Plain text version';
    ruling.rulingTextHtml = '<p>Formatted HTML version</p>';
    render(<RulingDetail ruling={ruling} />);

    // Should show HTML version, not plain text
    expect(screen.getByText('Formatted HTML version')).toBeInTheDocument();
    expect(screen.queryByText('Plain text version')).not.toBeInTheDocument();
  });

  it('falls back to plain text when rulingTextHtml is null', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = null;
    ruling.rulingText = 'Fallback plain text.\n\nSecond paragraph.';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.getByText('Fallback plain text.')).toBeInTheDocument();
    expect(screen.getByText('Second paragraph.')).toBeInTheDocument();
  });

  it('does not render ruling text section when both rulingText and rulingTextHtml are null', () => {
    const ruling = buildFullRuling();
    ruling.rulingText = null;
    ruling.rulingTextHtml = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Ruling Text')).not.toBeInTheDocument();
  });

  it('sanitizes dangerous HTML content from rulingTextHtml', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Safe content</p><script>alert("xss")</script><p onclick="alert(1)">More content</p>';
    const { container } = render(<RulingDetail ruling={ruling} />);

    const formattedDiv = container.querySelector('.ruling-formatted');
    expect(formattedDiv).toBeInTheDocument();
    // Script tag should be stripped
    expect(formattedDiv?.innerHTML).not.toContain('<script>');
    expect(formattedDiv?.innerHTML).not.toContain('alert');
    // Safe content should remain
    expect(formattedDiv?.innerHTML).toContain('Safe content');
    expect(formattedDiv?.innerHTML).toContain('More content');
    // onclick should be stripped
    expect(formattedDiv?.innerHTML).not.toContain('onclick');
  });

  it('renders formatted HTML with ruling template CSS classes', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = [
      '<div class="ruling">',
      '  <header class="ruling-header">',
      '    <div class="case-number">Case No. 24STCV16071</div>',
      '    <div class="case-caption">',
      '      <span class="party plaintiff">Alexis Maxwell</span>',
      '      <span class="vs">v.</span>',
      '      <span class="party defendant">650 S Spring Owner LLC</span>',
      '    </div>',
      '  </header>',
      '  <section class="ruling-body">',
      '    <h3>Motion to Compel Arbitration</h3>',
      '    <p>The motion is GRANTED.</p>',
      '  </section>',
      '</div>',
    ].join('\n');
    const { container } = render(<RulingDetail ruling={ruling} />);

    // Verify structure is preserved
    const formattedDiv = container.querySelector('.ruling-formatted');
    expect(formattedDiv?.querySelector('.ruling')).toBeInTheDocument();
    expect(formattedDiv?.querySelector('.ruling-header')).toBeInTheDocument();
    expect(formattedDiv?.querySelector('.case-number')).toBeInTheDocument();
    expect(formattedDiv?.querySelector('.case-caption')).toBeInTheDocument();
    expect(formattedDiv?.querySelector('.party.plaintiff')).toBeInTheDocument();
    expect(formattedDiv?.querySelector('.party.defendant')).toBeInTheDocument();
    expect(formattedDiv?.querySelector('.vs')).toBeInTheDocument();
    expect(formattedDiv?.querySelector('.ruling-body')).toBeInTheDocument();
  });
});
