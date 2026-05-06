'use client';

import { useState, useRef, useCallback, useEffect, useId } from 'react';

export interface AutocompleteProps {
  /** The current input value (controlled). */
  value: string;
  /** Called when the value changes (typing or selecting a suggestion). */
  onChange: (value: string) => void;
  /** The list of all possible suggestions. Filtered client-side. */
  options: string[];
  /** Placeholder text for the input. */
  placeholder?: string;
  /** HTML id for the input element. */
  id?: string;
  /** Additional CSS class names for the input. */
  className?: string;
  /** aria-label for the input. */
  'aria-label'?: string;
}

/**
 * An accessible autocomplete/combobox component.
 *
 * Implements the ARIA combobox pattern with:
 * - Case-insensitive substring matching
 * - Keyboard navigation (ArrowUp, ArrowDown, Enter, Escape)
 * - Free-text input (user is not forced to pick from the list)
 * - Light and dark mode support via Tailwind
 */
export function Autocomplete({
  value,
  onChange,
  options,
  placeholder,
  id,
  className,
  'aria-label': ariaLabel,
}: AutocompleteProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxRef = useRef<HTMLUListElement>(null);
  const blurTimeoutRef = useRef<ReturnType<typeof setTimeout>>();
  const generatedId = useId();
  const listboxId = `${id ?? generatedId}-listbox`;

  // Filter options by case-insensitive substring match
  const filtered = value
    ? options.filter((opt) => opt.toLowerCase().includes(value.toLowerCase()))
    : options;

  const showSuggestions = isOpen && filtered.length > 0;

  // Reset active index when filtered results change
  useEffect(() => {
    setActiveIndex(-1);
  }, [value]);

  // Clean up blur timeout on unmount
  useEffect(() => {
    return () => {
      if (blurTimeoutRef.current) clearTimeout(blurTimeoutRef.current);
    };
  }, []);

  // Scroll active item into view
  useEffect(() => {
    if (activeIndex >= 0 && listboxRef.current) {
      const item = listboxRef.current.children[activeIndex] as HTMLElement | undefined;
      item?.scrollIntoView?.({ block: 'nearest' });
    }
  }, [activeIndex]);

  const selectOption = useCallback(
    (opt: string) => {
      if (blurTimeoutRef.current) clearTimeout(blurTimeoutRef.current);
      onChange(opt);
      setIsOpen(false);
      setActiveIndex(-1);
      inputRef.current?.focus();
    },
    [onChange],
  );

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      onChange(e.target.value);
      setIsOpen(true);
    },
    [onChange],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (!showSuggestions) {
        // Open the list on arrow down even if closed
        if (e.key === 'ArrowDown' && filtered.length > 0) {
          e.preventDefault();
          setIsOpen(true);
          setActiveIndex(0);
        }
        return;
      }

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          setActiveIndex((prev) => (prev < filtered.length - 1 ? prev + 1 : 0));
          break;
        case 'ArrowUp':
          e.preventDefault();
          setActiveIndex((prev) => (prev > 0 ? prev - 1 : filtered.length - 1));
          break;
        case 'Enter':
          if (activeIndex >= 0 && activeIndex < filtered.length) {
            e.preventDefault();
            selectOption(filtered[activeIndex]);
          }
          break;
        case 'Escape':
          e.preventDefault();
          setIsOpen(false);
          setActiveIndex(-1);
          break;
      }
    },
    [showSuggestions, filtered, activeIndex, selectOption],
  );

  const handleFocus = useCallback(() => {
    if (blurTimeoutRef.current) clearTimeout(blurTimeoutRef.current);
    setIsOpen(true);
  }, []);

  const handleBlur = useCallback(() => {
    // Delay closing so click on option can register
    blurTimeoutRef.current = setTimeout(() => {
      setIsOpen(false);
      setActiveIndex(-1);
    }, 150);
  }, []);

  return (
    <div className="relative">
      <input
        ref={inputRef}
        id={id}
        type="text"
        role="combobox"
        aria-expanded={showSuggestions}
        aria-autocomplete="list"
        aria-controls={showSuggestions ? listboxId : undefined}
        aria-activedescendant={
          showSuggestions && activeIndex >= 0
            ? `${listboxId}-option-${activeIndex}`
            : undefined
        }
        aria-label={ariaLabel}
        autoComplete="off"
        value={value}
        onChange={handleInputChange}
        onKeyDown={handleKeyDown}
        onFocus={handleFocus}
        onBlur={handleBlur}
        placeholder={placeholder}
        className={className}
      />
      {showSuggestions && (
        <ul
          ref={listboxRef}
          id={listboxId}
          role="listbox"
          className="absolute z-50 mt-1 max-h-60 w-full overflow-auto rounded-md border border-border bg-popover shadow-lg"
        >
          {filtered.map((opt, i) => (
            <li
              key={opt}
              id={`${listboxId}-option-${i}`}
              role="option"
              aria-selected={i === activeIndex}
              onMouseDown={(e) => {
                e.preventDefault();
                selectOption(opt);
              }}
              className={`cursor-pointer px-3 py-2 text-sm ${
                i === activeIndex
                  ? 'bg-brand-accent-surface text-amber-900 dark:bg-amber-900/30 dark:text-amber-100'
                  // shadcn-accent: intentional - text-popover-foreground sits on the parent <ul>'s bg-popover surface (line 174); hover:bg-accent is the canonical shadcn hover idiom (#4225).
                  : 'text-popover-foreground hover:bg-accent'
              }`}
            >
              {opt}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
