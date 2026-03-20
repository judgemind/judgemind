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

  it('returns raw HTML during SSR (window undefined)', async () => {
    // Simulate server environment by removing window
    // @ts-expect-error -- intentionally removing window to simulate SSR
    delete globalThis.window;

    const { sanitizeRulingHtml } = await import('../sanitize-html');
    const html = '<p>Raw content</p><script>alert("xss")</script>';
    const result = sanitizeRulingHtml(html);
    // On the server, the HTML should be returned as-is
    expect(result).toBe(html);
  });
});
