import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';

describe('shadcn/ui setup', () => {
  describe('cn() utility', () => {
    it('merges class names', () => {
      expect(cn('px-2 py-1', 'px-4')).toBe('py-1 px-4');
    });

    it('handles conditional classes', () => {
      expect(cn('base', false && 'hidden', 'extra')).toBe('base extra');
    });

    it('returns empty string for no inputs', () => {
      expect(cn()).toBe('');
    });
  });

  describe('Button component', () => {
    it('renders with default variant', () => {
      render(<Button>Click me</Button>);
      const button = screen.getByRole('button', { name: 'Click me' });
      expect(button).toBeInTheDocument();
      expect(button.tagName).toBe('BUTTON');
    });

    it('renders with destructive variant', () => {
      render(<Button variant="destructive">Delete</Button>);
      const button = screen.getByRole('button', { name: 'Delete' });
      expect(button).toBeInTheDocument();
      expect(button.className).toContain('bg-destructive');
    });

    it('renders with custom className', () => {
      render(<Button className="my-custom-class">Custom</Button>);
      const button = screen.getByRole('button', { name: 'Custom' });
      expect(button.className).toContain('my-custom-class');
    });

    it('supports asChild prop', () => {
      render(
        <Button asChild>
          <a href="/test">Link Button</a>
        </Button>,
      );
      const link = screen.getByRole('link', { name: 'Link Button' });
      expect(link).toBeInTheDocument();
      expect(link.tagName).toBe('A');
    });
  });

  describe('Card component', () => {
    it('renders card with header and content', () => {
      render(
        <Card>
          <CardHeader>
            <CardTitle>Test Title</CardTitle>
          </CardHeader>
          <CardContent>
            <p>Card body content</p>
          </CardContent>
        </Card>,
      );

      expect(screen.getByText('Test Title')).toBeInTheDocument();
      expect(screen.getByText('Card body content')).toBeInTheDocument();
    });

    it('applies card styling classes', () => {
      const { container } = render(<Card data-testid="card">Content</Card>);
      const card = container.firstElementChild;
      expect(card?.className).toContain('rounded-lg');
      expect(card?.className).toContain('border');
      expect(card?.className).toContain('shadow-sm');
    });
  });
});
