import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RulingDetail } from '../RulingDetail';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';

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

  it('does not render ruling text section when both rulingText and rulingTextHtml are null', () => {
    const ruling = buildFullRuling();
    ruling.rulingText = null;
    ruling.rulingTextHtml = null;
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
    ruling.rulingTextHtml = '<div class="ruling"><div class="ruling-body"><p>The motion is <strong>GRANTED</strong>.</p></div></div>';
    ruling.rulingText = 'The motion is GRANTED.';
    const { container } = render(<RulingDetail ruling={ruling} />);

    // Should render the HTML content
    expect(screen.getByText('Ruling Text')).toBeInTheDocument();
    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('<strong>GRANTED</strong>');

    // Should have the ruling-body class from the template
    expect(container.querySelector('.ruling-body')).toBeInTheDocument();
  });

  it('falls back to raw text when rulingTextHtml is null', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = null;
    ruling.rulingText = 'The motion is granted.\n\nPlaintiff wins.';
    const { container } = render(<RulingDetail ruling={ruling} />);

    // Should render paragraphs from cleanRulingText
    expect(screen.getByText('Ruling Text')).toBeInTheDocument();
    expect(screen.getByText('The motion is granted.')).toBeInTheDocument();
    expect(screen.getByText('Plaintiff wins.')).toBeInTheDocument();

    // Should NOT have the ruling-content wrapper with dangerouslySetInnerHTML
    expect(container.querySelector('.ruling-content')).not.toBeInTheDocument();
  });

  it('prefers rulingTextHtml over rulingText when both are present', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Formatted version</p>';
    ruling.rulingText = 'Raw version';
    const { container } = render(<RulingDetail ruling={ruling} />);

    expect(container.querySelector('.ruling-content')).toBeInTheDocument();
    expect(screen.getByText('Formatted version')).toBeInTheDocument();
    expect(screen.queryByText('Raw version')).not.toBeInTheDocument();
  });

  it('renders ruling text section when only rulingTextHtml is present (rulingText is null)', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Only HTML available</p>';
    ruling.rulingText = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.getByText('Ruling Text')).toBeInTheDocument();
    expect(screen.getByText('Only HTML available')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // HTML sanitization
  // -------------------------------------------------------------------------

  it('strips script tags from rulingTextHtml', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Safe content</p><script>alert("xss")</script>';
    const { container } = render(<RulingDetail ruling={ruling} />);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('Safe content');
    expect(rulingContent?.innerHTML).not.toContain('script');
    expect(rulingContent?.innerHTML).not.toContain('alert');
  });

  it('strips event handler attributes from rulingTextHtml', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p onerror="alert(1)" onclick="alert(2)">Content</p>';
    const { container } = render(<RulingDetail ruling={ruling} />);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).not.toContain('onerror');
    expect(rulingContent?.innerHTML).not.toContain('onclick');
    expect(rulingContent?.innerHTML).toContain('Content');
  });

  it('strips iframe tags from rulingTextHtml', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Before</p><iframe src="https://evil.com"></iframe><p>After</p>';
    const { container } = render(<RulingDetail ruling={ruling} />);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent?.innerHTML).not.toContain('iframe');
    expect(rulingContent?.innerHTML).toContain('Before');
    expect(rulingContent?.innerHTML).toContain('After');
  });

  it('strips style attributes but preserves class attributes', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<div class="ruling" style="background:red"><p>Content</p></div>';
    const { container } = render(<RulingDetail ruling={ruling} />);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent?.innerHTML).toContain('class="ruling"');
    expect(rulingContent?.innerHTML).not.toContain('style');
  });

  it('strips form and input elements from rulingTextHtml', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Content</p><form action="/steal"><input type="text" /></form>';
    const { container } = render(<RulingDetail ruling={ruling} />);

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent?.innerHTML).not.toContain('form');
    expect(rulingContent?.innerHTML).not.toContain('input');
    expect(rulingContent?.innerHTML).toContain('Content');
  });
});

// ---------------------------------------------------------------------------
// sanitizeRulingHtml unit tests
// ---------------------------------------------------------------------------

describe('sanitizeRulingHtml', () => {
  it('preserves allowed tags', () => {
    const html = '<div class="ruling"><p>Text</p><strong>Bold</strong><em>Italic</em></div>';
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('<div class="ruling">');
    expect(result).toContain('<p>');
    expect(result).toContain('<strong>');
    expect(result).toContain('<em>');
  });

  it('preserves class attributes', () => {
    const html = '<div class="ruling-body"><span class="case-number">123</span></div>';
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('class="ruling-body"');
    expect(result).toContain('class="case-number"');
  });

  it('removes script tags', () => {
    const html = '<p>Safe</p><script>alert("xss")</script>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('script');
    expect(result).toContain('Safe');
  });

  it('removes event handler attributes', () => {
    const html = '<p onmouseover="alert(1)">Hover</p>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('onmouseover');
    expect(result).toContain('Hover');
  });

  it('removes style attributes', () => {
    const html = '<p style="color:red">Styled</p>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('style');
    expect(result).toContain('Styled');
  });

  it('removes disallowed tags like img', () => {
    const html = '<p>Text</p><img src="x" onerror="alert(1)" />';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('img');
    expect(result).toContain('Text');
  });

  it('preserves list elements', () => {
    const html = '<ul><li>Item 1</li><li>Item 2</li></ul>';
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('<ul>');
    expect(result).toContain('<li>');
    expect(result).toContain('Item 1');
  });

  it('preserves heading elements', () => {
    const html = '<h3>Motion for Summary Judgment</h3>';
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('<h3>');
    expect(result).toContain('Motion for Summary Judgment');
  });

  it('returns empty string for empty input', () => {
    expect(sanitizeRulingHtml('')).toBe('');
  });
});
