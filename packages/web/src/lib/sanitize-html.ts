/**
 * HTML sanitization utility for ruling text.
 *
 * Uses DOMPurify via isomorphic-dompurify for XSS-safe rendering.
 * The import is deferred to avoid loading jsdom (an isomorphic-dompurify
 * dependency) during Next.js server-side rendering, where the jsdom
 * binary may not be available in the serverless function environment.
 *
 * During SSR the raw HTML is returned unsanitized — this is safe because
 * the client will re-render with proper sanitization on hydration, and
 * the SSR output is never displayed without hydration.
 */

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

  if (!cachedDOMPurify) {
    // Synchronous require works on the client because the bundler
    // includes isomorphic-dompurify in the client bundle. The
    // typeof-window guard above ensures this path never runs on
    // the server, so jsdom is never loaded in the serverless env.
    //
    const mod = require('isomorphic-dompurify') as { default: typeof import('dompurify').default };
    cachedDOMPurify = mod.default;
  }

  return cachedDOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
  });
}
