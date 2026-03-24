'use client';

import { useState, useCallback, useRef } from 'react';
import { buildDocumentContentUrl, buildDownloadUrl, cleanRulingText, cleanSummary, FORMAT_LABELS, stripMetadataHeaderHtml, type RulingMetadata } from '@/lib/display-helpers';
import { SECTION_HEADING } from '@/lib/typography';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';

interface RulingProps {
  /** Pre-sanitized HTML from the server component (already run through DOMPurify). */
  sanitizedRulingTextHtml?: string | null;
  ruling: {
    id: string;
    hearingDate: string;
    outcome: string | null;
    motionType: string | null;
    isTentative: boolean;
    department: string | null;
    rulingText: string | null;
    rulingTextHtml: string | null;
    summary: string | null;
    postedAt: string | null;
    documentId: string | null;
    documentFormat: string | null;
    case: {
      id: string;
      caseNumber: string;
      caseTitle: string | null;
    } | null;
    judge: {
      id: string;
      canonicalName: string;
    } | null;
    court: {
      courtName: string;
      county: string;
    } | null;
  };
}

/** State for the HTML document viewer. */
type ViewerState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; html: string }
  | { status: 'error'; message: string };

export function RulingDetail({ ruling, sanitizedRulingTextHtml }: RulingProps) {
  // Build metadata context for stripping redundant header boilerplate
  const metadata: RulingMetadata = {
    caseNumber: ruling.case?.caseNumber,
    caseTitle: ruling.case?.caseTitle ?? undefined,
    judgeName: ruling.judge?.canonicalName,
    department: ruling.department ?? undefined,
    hearingDate: ruling.hearingDate,
    motionType: ruling.motionType ?? undefined,
  };
  const displaySummary = ruling.summary ? cleanSummary(ruling.summary) : null;

  const isHtmlDocument = ruling.documentFormat === 'html';

  const [viewerState, setViewerState] = useState<ViewerState>({ status: 'idle' });

  // Ref mirrors latest viewerState so the stable callback can read current
  // values without capturing stale closures or depending on viewerState.
  const viewerStateRef = useRef(viewerState);
  viewerStateRef.current = viewerState;

  const handleViewOriginal = useCallback(async () => {
    if (!ruling.documentId) return;

    if (viewerStateRef.current.status === 'loaded') {
      // Toggle off — hide the viewer
      setViewerState({ status: 'idle' });
      return;
    }

    if (viewerStateRef.current.status === 'loading') {
      // Already loading — ignore duplicate clicks
      return;
    }

    setViewerState({ status: 'loading' });

    try {
      const url = buildDocumentContentUrl(ruling.documentId);
      const response = await fetch(url);

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        const errorMessage = errorData?.error ?? `Failed to load document (HTTP ${response.status})`;
        setViewerState({ status: 'error', message: errorMessage });
        return;
      }

      const html = await response.text();
      setViewerState({ status: 'loaded', html });
    } catch {
      setViewerState({ status: 'error', message: 'Failed to load document. Please try again.' });
    }
  }, [ruling.documentId]);

  return (
    <div className="space-y-6">
      {/* Summary (AI-generated) — highlighted in a Card */}
      {displaySummary && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className={SECTION_HEADING}>
              Summary
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm leading-relaxed text-foreground">
              {displaySummary}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Ruling text in its own Card */}
      {(sanitizedRulingTextHtml || ruling.rulingText) && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className={SECTION_HEADING}>
              Ruling Text
            </CardTitle>
            {ruling.documentId && (
              <div className="flex items-center gap-2">
                {isHtmlDocument && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleViewOriginal}
                    disabled={viewerState.status === 'loading'}
                    data-testid="view-original-button"
                  >
                    {viewerState.status === 'loading'
                      ? 'Loading\u2026'
                      : viewerState.status === 'loaded'
                        ? 'Hide original'
                        : 'View original'}
                  </Button>
                )}
                <Button variant="outline" size="sm" asChild>
                  <a
                    href={buildDownloadUrl(ruling.documentId)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Download original document
                    {ruling.documentFormat && (
                      <span className="ml-1.5 rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase text-muted-foreground">
                        {FORMAT_LABELS[ruling.documentFormat] ??
                          ruling.documentFormat.toUpperCase()}
                      </span>
                    )}
                  </a>
                </Button>
              </div>
            )}
          </CardHeader>

          {/* Original HTML document viewer (sandboxed iframe) */}
          {viewerState.status === 'loaded' && (
            <CardContent className="border-t pt-4">
              <iframe
                srcDoc={viewerState.html}
                sandbox=""
                className="w-full rounded border"
                style={{ height: '600px' }}
                title="Original court document"
                data-testid="original-document-iframe"
              />
            </CardContent>
          )}

          {/* Error state */}
          {viewerState.status === 'error' && (
            <CardContent className="border-t pt-4">
              <p className="text-sm text-destructive" data-testid="viewer-error">
                {viewerState.message}
              </p>
            </CardContent>
          )}

          <CardContent className="prose prose-slate dark:prose-invert max-w-none">
            {sanitizedRulingTextHtml ? (
              <div
                className="ruling-content text-sm leading-relaxed text-foreground"
                dangerouslySetInnerHTML={{
                  __html: stripMetadataHeaderHtml(sanitizedRulingTextHtml, metadata),
                }}
              />
            ) : (
              <div className="space-y-4">
                {cleanRulingText(ruling.rulingText!, metadata).map((paragraph, idx) => (
                  <p
                    key={idx}
                    className="text-sm leading-relaxed text-foreground"
                  >
                    {paragraph}
                  </p>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Document download — shown standalone when there is no ruling text */}
      {ruling.documentId && !sanitizedRulingTextHtml && !ruling.rulingText && (
        <section>
          <div className="flex items-center gap-2">
            {isHtmlDocument && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleViewOriginal}
                disabled={viewerState.status === 'loading'}
                data-testid="view-original-button"
              >
                {viewerState.status === 'loading'
                  ? 'Loading\u2026'
                  : viewerState.status === 'loaded'
                    ? 'Hide original'
                    : 'View original'}
              </Button>
            )}
            <Button variant="outline" size="sm" asChild>
              <a
                href={buildDownloadUrl(ruling.documentId)}
                target="_blank"
                rel="noopener noreferrer"
              >
                Download original document
                {ruling.documentFormat && (
                  <span className="ml-1.5 rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase text-muted-foreground">
                    {FORMAT_LABELS[ruling.documentFormat] ??
                      ruling.documentFormat.toUpperCase()}
                  </span>
                )}
              </a>
            </Button>
          </div>

          {/* Original HTML document viewer (sandboxed iframe) — standalone */}
          {viewerState.status === 'loaded' && (
            <div className="mt-4">
              <iframe
                srcDoc={viewerState.html}
                sandbox=""
                className="w-full rounded border"
                style={{ height: '600px' }}
                title="Original court document"
                data-testid="original-document-iframe"
              />
            </div>
          )}

          {/* Error state — standalone */}
          {viewerState.status === 'error' && (
            <p className="mt-4 text-sm text-destructive" data-testid="viewer-error">
              {viewerState.message}
            </p>
          )}
        </section>
      )}
    </div>
  );
}
