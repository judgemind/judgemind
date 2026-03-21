import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('sanitizeRulingHtml', () => {
  let originalWindow: typeof globalThis.window;

  beforeEach(() => {
    // Save original window reference (jsdom provides it in vitest)
    originalWindow = globalThis.window;
    vi.resetModules();
  });

  afterEach(() => {
    // Restore window for other tests
    if (originalWindow) {
      Object.defineProperty(globalThis, 'window', {
        value: originalWindow,
        writable: true,
        configurable: true,
      });
    }
  });

  it('sanitizes HTML on the client (window defined)', async () => {
    // window is defined in jsdom (vitest default), so this tests the client path
    const { sanitizeRulingHtml } = await import('../sanitize-html');
    const result = sanitizeRulingHtml('<p>Safe</p><script>alert("xss")</script>');
    expect(result).toContain('Safe');
    expect(result).not.toContain('script');
  });

  it('preserves allowed tags on the client', async () => {
    const { sanitizeRulingHtml } = await import('../sanitize-html');
    const html = '<div class="ruling"><p>Text</p><strong>Bold</strong></div>';
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('<div class="ruling">');
    expect(result).toContain('<p>');
    expect(result).toContain('<strong>');
  });

  it('removes disallowed attributes on the client', async () => {
    const { sanitizeRulingHtml } = await import('../sanitize-html');
    const result = sanitizeRulingHtml('<p style="color:red" onclick="alert(1)">Text</p>');
    expect(result).not.toContain('style');
    expect(result).not.toContain('onclick');
    expect(result).toContain('Text');
  });

  it('returns empty string for empty input on the client', async () => {
    const { sanitizeRulingHtml } = await import('../sanitize-html');
    expect(sanitizeRulingHtml('')).toBe('');
  });

  it('sanitizes HTML during SSR (window undefined)', async () => {
    // Simulate server environment by removing window
    // @ts-expect-error -- intentionally removing window to simulate SSR
    delete globalThis.window;

    const { sanitizeRulingHtml } = await import('../sanitize-html');
    const html = '<p>Safe content</p><script>alert("xss")</script>';
    const result = sanitizeRulingHtml(html);
    // SSR now sanitizes too — script tags must be stripped
    expect(result).toContain('Safe content');
    expect(result).toContain('<p>');
    expect(result).not.toContain('script');
    expect(result).not.toContain('alert');
  });
});

describe('sanitizeExcerptHtml', () => {
  let originalWindow: typeof globalThis.window;

  beforeEach(() => {
    originalWindow = globalThis.window;
    vi.resetModules();
  });

  afterEach(() => {
    if (originalWindow) {
      Object.defineProperty(globalThis, 'window', {
        value: originalWindow,
        writable: true,
        configurable: true,
      });
    }
  });

  it('preserves <em> tags used for search highlighting', async () => {
    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    const html = 'The motion for <em>summary judgment</em> is granted';
    const result = sanitizeExcerptHtml(html);
    expect(result).toContain('<em>summary judgment</em>');
  });

  it('preserves <mark> tags used for search highlighting', async () => {
    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    const html = 'The <mark>defendant</mark> filed a response';
    const result = sanitizeExcerptHtml(html);
    expect(result).toContain('<mark>defendant</mark>');
  });

  it('strips <script> tags', async () => {
    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    const html = 'Safe text<script>alert("xss")</script>';
    const result = sanitizeExcerptHtml(html);
    expect(result).toContain('Safe text');
    expect(result).not.toContain('script');
    expect(result).not.toContain('alert');
  });

  it('strips <img> tags with onerror handlers', async () => {
    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    const html = 'Text <img src="x" onerror="alert(1)"> more text';
    const result = sanitizeExcerptHtml(html);
    expect(result).not.toContain('img');
    expect(result).not.toContain('onerror');
    expect(result).toContain('Text');
    expect(result).toContain('more text');
  });

  it('strips structural HTML tags like <div> and <p>', async () => {
    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    const html = '<div><p>Some <em>highlighted</em> text</p></div>';
    const result = sanitizeExcerptHtml(html);
    expect(result).not.toContain('<div>');
    expect(result).not.toContain('<p>');
    expect(result).toContain('<em>highlighted</em>');
    expect(result).toContain('Some');
    expect(result).toContain('text');
  });

  it('strips attributes from allowed tags', async () => {
    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    const html = '<em class="highlight" onclick="alert(1)">term</em>';
    const result = sanitizeExcerptHtml(html);
    expect(result).toContain('<em>term</em>');
    expect(result).not.toContain('class');
    expect(result).not.toContain('onclick');
  });

  it('returns empty string for empty input', async () => {
    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    expect(sanitizeExcerptHtml('')).toBe('');
  });

  it('sanitizes HTML during SSR (window undefined)', async () => {
    // @ts-expect-error -- intentionally removing window to simulate SSR
    delete globalThis.window;

    const { sanitizeExcerptHtml } = await import('../sanitize-html');
    const html = 'Unsafe <script>alert("xss")</script>';
    const result = sanitizeExcerptHtml(html);
    // Excerpt sanitization runs on the server too
    expect(result).not.toContain('script');
    expect(result).not.toContain('alert');
    expect(result).toContain('Unsafe');
  });
});
