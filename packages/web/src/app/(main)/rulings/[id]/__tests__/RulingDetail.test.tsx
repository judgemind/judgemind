import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { RulingDetail } from '../RulingDetail';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';

// next/link mock removed — RulingDetail no longer uses Link (#1515)

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

    // Case and Judge sections are rendered in the server component metadata card,
    // not in RulingDetail (removed as part of #1515 deduplication)
    expect(screen.queryByText('Case')).not.toBeInTheDocument();
    expect(screen.queryByText('Judge')).not.toBeInTheDocument();

    // Summary section (now in a Card)
    expect(screen.getByText('Summary')).toBeInTheDocument();
    expect(screen.getByText('Court grants MSJ in favor of plaintiff.')).toBeInTheDocument();

    // Ruling text section (now in a Card)
    expect(screen.getByText('Ruling Text')).toBeInTheDocument();

    // Document download button
    expect(screen.getByText('Download original document')).toBeInTheDocument();
    expect(screen.getByText('PDF')).toBeInTheDocument();
  });

  // Case and Judge links are now in the server component metadata card (page.tsx),
  // not in RulingDetail. See page.test.tsx for link tests. (#1515)

  it('renders download button with correct href from buildDownloadUrl in ruling text header', () => {
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

  // Case and Judge null-handling tests removed — those sections are now in the
  // server component metadata card (page.tsx), not in RulingDetail. (#1515)

  it('does not render summary section when summary is null', () => {
    const ruling = buildFullRuling();
    ruling.summary = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Summary')).not.toBeInTheDocument();
  });

  it('does not render ruling text section when both rulingText and sanitizedRulingTextHtml are null', () => {
    const ruling = buildFullRuling();
    ruling.rulingText = null;
    ruling.rulingTextHtml = null;
    render(<RulingDetail ruling={ruling} sanitizedRulingTextHtml={null} />);

    expect(screen.queryByText('Ruling Text')).not.toBeInTheDocument();
  });

  it('does not render download button when documentId is null', () => {
    const ruling = buildFullRuling();
    ruling.documentId = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText('Download original document')).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Download link placement (#1236)
  // -------------------------------------------------------------------------

  it('renders download button inside the ruling text card header when ruling text exists', () => {
    const ruling = buildFullRuling();
    const { container } = render(<RulingDetail ruling={ruling} />);

    // The CardHeader containing "Ruling Text" should also contain the download link
    const rulingTextHeading = screen.getByText('Ruling Text');
    const cardHeader = rulingTextHeading.parentElement;
    expect(cardHeader).toBeInTheDocument();
    // Download link should be a descendant of the same card header
    const downloadLink = screen.getByText('Download original document').closest('a');
    expect(cardHeader!.contains(downloadLink)).toBe(true);
  });

  it('renders standalone download button when there is no ruling text but a document exists', () => {
    const ruling = buildFullRuling();
    ruling.rulingText = null;
    ruling.rulingTextHtml = null;
    render(<RulingDetail ruling={ruling} sanitizedRulingTextHtml={null} />);

    // Download button should still be visible even without ruling text
    expect(screen.getByText('Download original document')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Summary cleanup (#1236)
  // -------------------------------------------------------------------------

  it('strips "# Summary" heading from summary text', () => {
    const ruling = buildFullRuling();
    ruling.summary = '# Summary\nCourt grants MSJ in favor of plaintiff.';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText(/# Summary/)).not.toBeInTheDocument();
    expect(screen.getByText('Court grants MSJ in favor of plaintiff.')).toBeInTheDocument();
  });

  it('strips redundant metadata lines from summary', () => {
    const ruling = buildFullRuling();
    ruling.summary = 'Case Number: 25STCV12345\nJudge: Johnson\nThe ruling is favorable to plaintiff.';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByText(/Case Number:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Judge:/)).not.toBeInTheDocument();
    expect(screen.getByText('The ruling is favorable to plaintiff.')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Minimal data — all nullable fields null
  // -------------------------------------------------------------------------

  it('renders only the wrapper div when all nullable fields are null', () => {
    const ruling = buildMinimalRuling();
    const { container } = render(<RulingDetail ruling={ruling} />);

    // No section headings should be rendered (Case/Judge are in page.tsx metadata card)
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

  // Case number edge case tests removed — case display is now in the server
  // component metadata card (page.tsx), not in RulingDetail. (#1515)

  it('renders download button without format badge when documentFormat is null', () => {
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

  // Case number em-dash test removed — case display is now in the server
  // component metadata card (page.tsx), not in RulingDetail. (#1515)

  // -------------------------------------------------------------------------
  // Formatted HTML rendering (sanitizedRulingTextHtml prop)
  // -------------------------------------------------------------------------

  it('renders pre-sanitized HTML when sanitizedRulingTextHtml is provided', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<div class="ruling"><div class="ruling-body"><p>The motion is <strong>GRANTED</strong>.</p></div></div>';
    ruling.rulingText = 'The motion is GRANTED.';
    // Simulate what the server component does: sanitize before passing
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    // Should render the HTML content
    expect(screen.getByText('Ruling Text')).toBeInTheDocument();
    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('<strong>GRANTED</strong>');

    // Should have the ruling-body class from the template
    expect(container.querySelector('.ruling-body')).toBeInTheDocument();
  });

  it('falls back to raw text when sanitizedRulingTextHtml is null', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = null;
    ruling.rulingText = 'The motion is granted.\n\nPlaintiff wins.';
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={null} />,
    );

    // Should render paragraphs from cleanRulingText
    expect(screen.getByText('Ruling Text')).toBeInTheDocument();
    expect(screen.getByText('The motion is granted.')).toBeInTheDocument();
    expect(screen.getByText('Plaintiff wins.')).toBeInTheDocument();

    // Should NOT have the ruling-content wrapper with dangerouslySetInnerHTML
    expect(container.querySelector('.ruling-content')).not.toBeInTheDocument();
  });

  it('prefers sanitizedRulingTextHtml over rulingText when both are present', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Formatted version</p>';
    ruling.rulingText = 'Raw version';
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    expect(container.querySelector('.ruling-content')).toBeInTheDocument();
    expect(screen.getByText('Formatted version')).toBeInTheDocument();
    expect(screen.queryByText('Raw version')).not.toBeInTheDocument();
  });

  it('renders ruling text section when only sanitizedRulingTextHtml is present (rulingText is null)', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Only HTML available</p>';
    ruling.rulingText = null;
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    expect(screen.getByText('Ruling Text')).toBeInTheDocument();
    expect(screen.getByText('Only HTML available')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // HTML sanitization (verified via sanitizedRulingTextHtml prop)
  // -------------------------------------------------------------------------

  it('renders sanitized HTML that has had script tags stripped', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Safe content</p><script>alert("xss")</script>';
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).toContain('Safe content');
    expect(rulingContent?.innerHTML).not.toContain('script');
    expect(rulingContent?.innerHTML).not.toContain('alert');
  });

  it('renders sanitized HTML that has had event handler attributes stripped', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p onerror="alert(1)" onclick="alert(2)">Content</p>';
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent).toBeInTheDocument();
    expect(rulingContent?.innerHTML).not.toContain('onerror');
    expect(rulingContent?.innerHTML).not.toContain('onclick');
    expect(rulingContent?.innerHTML).toContain('Content');
  });

  it('renders sanitized HTML that has had iframe tags stripped', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Before</p><iframe src="https://evil.com"></iframe><p>After</p>';
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent?.innerHTML).not.toContain('iframe');
    expect(rulingContent?.innerHTML).toContain('Before');
    expect(rulingContent?.innerHTML).toContain('After');
  });

  it('renders sanitized HTML that strips style but preserves class attributes', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<div class="ruling" style="background:red"><p>Content</p></div>';
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent?.innerHTML).toContain('class="ruling"');
    expect(rulingContent?.innerHTML).not.toContain('style');
  });

  it('renders sanitized HTML that strips form and input elements', () => {
    const ruling = buildFullRuling();
    ruling.rulingTextHtml = '<p>Content</p><form action="/steal"><input type="text" /></form>';
    const sanitized = sanitizeRulingHtml(ruling.rulingTextHtml);
    const { container } = render(
      <RulingDetail ruling={ruling} sanitizedRulingTextHtml={sanitized} />,
    );

    const rulingContent = container.querySelector('.ruling-content');
    expect(rulingContent?.innerHTML).not.toContain('form');
    expect(rulingContent?.innerHTML).not.toContain('input');
    expect(rulingContent?.innerHTML).toContain('Content');
  });

  // -------------------------------------------------------------------------
  // HTML document viewer (#1476)
  // -------------------------------------------------------------------------

  it('shows "View original" button for HTML-format documents', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.getByTestId('view-original-button')).toBeInTheDocument();
    expect(screen.getByTestId('view-original-button')).toHaveTextContent('View original');
  });

  it('does not show "View original" button for PDF-format documents', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'pdf';
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByTestId('view-original-button')).not.toBeInTheDocument();
  });

  it('does not show "View original" button when documentId is null', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    ruling.documentId = null;
    render(<RulingDetail ruling={ruling} />);

    expect(screen.queryByTestId('view-original-button')).not.toBeInTheDocument();
  });

  it('loads and displays iframe on "View original" click', async () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    const htmlContent = '<html><body><p>Court ruling content</p></body></html>';

    // Mock fetch
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(htmlContent, {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      }),
    );

    render(<RulingDetail ruling={ruling} />);

    // Click "View original"
    fireEvent.click(screen.getByTestId('view-original-button'));

    // Wait for iframe to appear
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe')).toBeInTheDocument();
    });

    const iframe = screen.getByTestId('original-document-iframe');
    expect(iframe).toHaveAttribute('sandbox', '');
    expect(iframe.getAttribute('srcdoc')).toBe(htmlContent);
    expect(iframe).toHaveAttribute('title', 'Original court document');

    // Button text should change to "Hide original"
    expect(screen.getByTestId('view-original-button')).toHaveTextContent('Hide original');

    fetchSpy.mockRestore();
  });

  it('hides iframe when "Hide original" is clicked', async () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    const htmlContent = '<html><body><p>Content</p></body></html>';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(htmlContent, { status: 200 }),
    );

    render(<RulingDetail ruling={ruling} />);

    // Click "View original"
    fireEvent.click(screen.getByTestId('view-original-button'));

    // Wait for iframe to appear
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe')).toBeInTheDocument();
    });

    // Click "Hide original"
    fireEvent.click(screen.getByTestId('view-original-button'));

    // Iframe should be gone
    expect(screen.queryByTestId('original-document-iframe')).not.toBeInTheDocument();
    // Button should revert to "View original"
    expect(screen.getByTestId('view-original-button')).toHaveTextContent('View original');

    fetchSpy.mockRestore();
  });

  it('shows error message when document content fetch fails', async () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'Document not found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    render(<RulingDetail ruling={ruling} />);

    fireEvent.click(screen.getByTestId('view-original-button'));

    await waitFor(() => {
      expect(screen.getByTestId('viewer-error')).toBeInTheDocument();
    });

    expect(screen.getByTestId('viewer-error')).toHaveTextContent('Document not found');

    fetchSpy.mockRestore();
  });

  it('shows error message on network failure', async () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(
      new Error('Network error'),
    );

    render(<RulingDetail ruling={ruling} />);

    fireEvent.click(screen.getByTestId('view-original-button'));

    await waitFor(() => {
      expect(screen.getByTestId('viewer-error')).toBeInTheDocument();
    });

    expect(screen.getByTestId('viewer-error')).toHaveTextContent('Failed to load document. Please try again.');

    fetchSpy.mockRestore();
  });

  it('keeps download link alongside "View original" for HTML documents', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    render(<RulingDetail ruling={ruling} />);

    // Both buttons should be present
    expect(screen.getByTestId('view-original-button')).toBeInTheDocument();
    expect(screen.getByText('Download original document')).toBeInTheDocument();
  });

  it('handles rapid toggle: load, hide, re-load without stale closure', async () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    const htmlContent = '<html><body><p>Content</p></body></html>';

    // Mock two successful fetches (one for initial load, one for re-load)
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(htmlContent, { status: 200 }))
      .mockResolvedValueOnce(new Response(htmlContent, { status: 200 }));

    render(<RulingDetail ruling={ruling} />);

    const btn = screen.getByTestId('view-original-button');

    // 1. Click "View original" — starts loading
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe')).toBeInTheDocument();
    });
    expect(btn).toHaveTextContent('Hide original');

    // 2. Click "Hide original" — toggles off
    fireEvent.click(btn);
    expect(screen.queryByTestId('original-document-iframe')).not.toBeInTheDocument();
    expect(btn).toHaveTextContent('View original');

    // 3. Click "View original" again — should load a second time, not stay stuck
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe')).toBeInTheDocument();
    });
    expect(btn).toHaveTextContent('Hide original');

    // Verify fetch was called twice (once per "View original" click)
    expect(fetchSpy).toHaveBeenCalledTimes(2);

    fetchSpy.mockRestore();
  });

  it('ignores duplicate clicks while loading (button is disabled)', async () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    const htmlContent = '<html><body><p>Content</p></body></html>';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(htmlContent, { status: 200 }),
    );

    render(<RulingDetail ruling={ruling} />);

    const btn = screen.getByTestId('view-original-button');

    // Fire two clicks back-to-back without awaiting — the button becomes
    // disabled after the first click triggers a re-render, so the second
    // click should be a no-op.
    fireEvent.click(btn);
    fireEvent.click(btn);

    // Wait for the document to load
    await waitFor(() => {
      expect(screen.getByTestId('original-document-iframe')).toBeInTheDocument();
    });

    // Only one fetch should have been made
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    fetchSpy.mockRestore();
  });

  it('shows "View original" button in standalone section when no ruling text exists but HTML doc does', () => {
    const ruling = buildFullRuling();
    ruling.documentFormat = 'html';
    ruling.rulingText = null;
    ruling.rulingTextHtml = null;
    render(<RulingDetail ruling={ruling} sanitizedRulingTextHtml={null} />);

    expect(screen.getByTestId('view-original-button')).toBeInTheDocument();
    expect(screen.getByText('Download original document')).toBeInTheDocument();
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
