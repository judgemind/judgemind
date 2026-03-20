/**
 * HTML sanitization utility for ruling text and search excerpts.
 *
 * Uses DOMPurify via isomorphic-dompurify for XSS-safe rendering.
 * The import is deferred to avoid loading jsdom (an isomorphic-dompurify
 * dependency) during Next.js server-side rendering, where the jsdom
 * binary may not be available in the serverless function environment.
 *
 * During SSR the raw HTML is returned unsanitized for ruling HTML — this
 * is safe because the client will re-render with proper sanitization on
 * hydration, and the SSR output is never displayed without hydration.
 *
 * Search excerpt sanitization runs on both server and client to ensure
 * XSS protection even if client-side hydration fails.
 */

/**
 * Tags allowed through DOMPurify sanitization for search excerpts.
 * Only highlighting tags needed by OpenSearch search results.
 */
const EXCERPT_ALLOWED_TAGS = ['em', 'mark'];

/**
 * Tags allowed through DOMPurify sanitization for ruling HTML.
 * Only structural and text-formatting tags needed by the ruling template.
 */
const ALLOWED_TAGS = [
  'div', 'section', 'article', 'header',
  'span', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'strong', 'em', 'b', 'i', 'u', 'br',
  'ul', 'ol', 'li',
];

/**
 * Attributes allowed through DOMPurify sanitization.
 * Only `class` is needed for CSS styling of ruling template elements.
 */
const ALLOWED_ATTR = ['class'];

/** Cached DOMPurify module to avoid repeated dynamic imports. */
let cachedDOMPurify: typeof import('isomorphic-dompurify').default | null = null;

/** Load the DOMPurify module (works on both client and server via isomorphic-dompurify). */
function getDOMPurify(): typeof import('isomorphic-dompurify').default {
  if (!cachedDOMPurify) {
    const mod = require('isomorphic-dompurify') as { default: typeof import('dompurify').default };
    cachedDOMPurify = mod.default;
  }
  return cachedDOMPurify;
}

/**
 * Sanitize ruling HTML, stripping all tags/attributes not in the allowlist.
 *
 * On the server (SSR), returns the HTML as-is to avoid loading jsdom.
 * On the client, uses DOMPurify for proper sanitization.
 */
export function sanitizeRulingHtml(html: string): string {
  if (typeof window === 'undefined') {
    // SSR: return as-is. The client will re-sanitize on hydration.
    return html;
  }

  return getDOMPurify().sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
  });
}

/**
 * Sanitize search excerpt HTML, allowing only highlighting tags (`em`, `mark`).
 *
 * Search excerpts come from OpenSearch highlighting and may contain arbitrary
 * HTML from scraped court content. This function strips everything except the
 * tags needed for search term highlighting.
 *
 * Unlike sanitizeRulingHtml, this function sanitizes on both server and client
 * to provide defense-in-depth against XSS even if client hydration fails.
 */
export function sanitizeExcerptHtml(html: string): string {
  return getDOMPurify().sanitize(html, {
    ALLOWED_TAGS: EXCERPT_ALLOWED_TAGS,
    ALLOWED_ATTR: [],
  });
}
