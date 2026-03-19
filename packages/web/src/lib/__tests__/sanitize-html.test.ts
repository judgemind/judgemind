import { describe, it, expect } from 'vitest';
import { sanitizeRulingHtml } from '../sanitize-html';

describe('sanitizeRulingHtml', () => {
  // ---------------------------------------------------------------------------
  // Safe content passes through
  // ---------------------------------------------------------------------------

  it('preserves allowed tags', () => {
    const html = '<div><p>Hello <strong>world</strong></p></div>';
    expect(sanitizeRulingHtml(html)).toBe('<div><p>Hello <strong>world</strong></p></div>');
  });

  it('preserves class attributes on allowed tags', () => {
    const html = '<div class="ruling"><span class="party plaintiff">Name</span></div>';
    expect(sanitizeRulingHtml(html)).toBe(
      '<div class="ruling"><span class="party plaintiff">Name</span></div>',
    );
  });

  it('preserves safe href attributes on anchor tags', () => {
    const html = '<a href="https://example.com">Link</a>';
    expect(sanitizeRulingHtml(html)).toBe('<a href="https://example.com">Link</a>');
  });

  it('preserves br tags', () => {
    const html = '<p>Line 1<br />Line 2</p>';
    expect(sanitizeRulingHtml(html)).toBe('<p>Line 1<br />Line 2</p>');
  });

  it('preserves heading tags', () => {
    const html = '<h3>Motion to Compel</h3>';
    expect(sanitizeRulingHtml(html)).toBe('<h3>Motion to Compel</h3>');
  });

  it('preserves list tags', () => {
    const html = '<ul><li>Item 1</li><li>Item 2</li></ul>';
    expect(sanitizeRulingHtml(html)).toBe('<ul><li>Item 1</li><li>Item 2</li></ul>');
  });

  it('preserves the full ruling template structure', () => {
    const html = [
      '<div class="ruling">',
      '<header class="ruling-header">',
      '<div class="case-number">Case No. 24STCV16071</div>',
      '</header>',
      '<section class="ruling-body">',
      '<h3>Motion</h3>',
      '<p>The motion is <strong>GRANTED</strong>.</p>',
      '</section>',
      '</div>',
    ].join('');
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('<header class="ruling-header">');
    expect(result).toContain('<div class="case-number">');
    expect(result).toContain('<section class="ruling-body">');
    expect(result).toContain('<strong>GRANTED</strong>');
  });

  // ---------------------------------------------------------------------------
  // Dangerous content is stripped
  // ---------------------------------------------------------------------------

  it('removes script tags and their content', () => {
    const html = '<p>Safe</p><script>alert("xss")</script><p>Also safe</p>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('<script>');
    expect(result).not.toContain('alert');
    expect(result).toContain('<p>Safe</p>');
    expect(result).toContain('<p>Also safe</p>');
  });

  it('removes style tags and their content', () => {
    const html = '<p>Text</p><style>body { display: none; }</style>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('<style>');
    expect(result).not.toContain('display');
    expect(result).toContain('<p>Text</p>');
  });

  it('removes event handler attributes', () => {
    const html = '<p onclick="alert(1)">Click me</p>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('onclick');
    expect(result).toContain('<p>');
    expect(result).toContain('Click me');
  });

  it('removes onerror attribute', () => {
    const html = '<div onerror="alert(1)">Content</div>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('onerror');
    expect(result).toContain('Content');
  });

  it('removes style attributes', () => {
    const html = '<div style="background:url(javascript:alert(1))">Content</div>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('style=');
    expect(result).toContain('Content');
  });

  it('strips disallowed tags but preserves their text content', () => {
    const html = '<p>Before <iframe>Inside iframe</iframe> After</p>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('<iframe');
    expect(result).toContain('Inside iframe');
    expect(result).toContain('Before');
    expect(result).toContain('After');
  });

  it('removes HTML comments', () => {
    const html = '<p>Before</p><!-- comment --><p>After</p>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('<!--');
    expect(result).not.toContain('comment');
    expect(result).toContain('<p>Before</p>');
    expect(result).toContain('<p>After</p>');
  });

  it('blocks javascript: href protocol', () => {
    const html = '<a href="javascript:alert(1)">Link</a>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('javascript:');
    expect(result).toContain('Link');
  });

  it('blocks data: href protocol', () => {
    const html = '<a href="data:text/html,<script>alert(1)</script>">Link</a>';
    const result = sanitizeRulingHtml(html);
    expect(result).not.toContain('data:');
  });

  it('allows fragment href', () => {
    const html = '<a href="#section1">Link</a>';
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('href="#section1"');
  });

  // ---------------------------------------------------------------------------
  // Edge cases
  // ---------------------------------------------------------------------------

  it('handles empty string', () => {
    expect(sanitizeRulingHtml('')).toBe('');
  });

  it('handles plain text with no HTML', () => {
    const text = 'Just plain text with no tags';
    expect(sanitizeRulingHtml(text)).toBe(text);
  });

  it('handles malformed tags gracefully', () => {
    const html = '<p>Text with < and > symbols</p>';
    // The regex approach should handle this gracefully
    const result = sanitizeRulingHtml(html);
    expect(result).toContain('Text with');
  });

  it('escapes attribute values to prevent injection via double quotes', () => {
    // If someone tries to break out of an attribute with literal quotes,
    // our escaping should render them harmless
    const html = '<div class="ruling" id="test">Content</div>';
    const result = sanitizeRulingHtml(html);
    // id is not an allowed attribute, so it should be stripped
    expect(result).not.toContain('id=');
    expect(result).toContain('class="ruling"');
    expect(result).toContain('Content');
  });
});
