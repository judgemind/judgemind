import { type ComponentPropsWithoutRef } from 'react';

type WordmarkSize = 'sm' | 'md' | 'lg';

const SIZE_CLASSES: Record<WordmarkSize, string> = {
  sm: 'text-lg font-semibold',
  md: 'text-xl font-semibold',
  lg: 'text-2xl font-bold',
};

interface WordmarkProps extends Omit<ComponentPropsWithoutRef<'span'>, 'children'> {
  /** Controls text size and weight. Defaults to "sm" (navbar). */
  size?: WordmarkSize;
}

/**
 * Brand wordmark: lowercase "judge" in default text color, "mind" in brand
 * accent color. Uses inline `<span>` elements so it scales and themes
 * naturally — no images involved.
 */
export function Wordmark({ size = 'sm', className, ...rest }: WordmarkProps) {
  const sizeClass = SIZE_CLASSES[size];

  return (
    <span className={`${sizeClass}${className ? ` ${className}` : ''}`} {...rest}>
      <span>judge</span>
      <span className="text-brand-accent dark:text-brand-accent-light">mind</span>
    </span>
  );
}
