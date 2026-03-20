import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Autocomplete } from '../src/components/Autocomplete';

const OPTIONS = ['Los Angeles', 'Orange', 'San Bernardino', 'San Francisco', 'Fresno'];

describe('Autocomplete', () => {
  let onChange: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onChange = vi.fn();
  });

  it('renders an input with combobox role', () => {
    render(
      <Autocomplete value="" onChange={onChange} options={OPTIONS} placeholder="Type here" />,
    );
    const input = screen.getByRole('combobox');
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('placeholder', 'Type here');
  });

  it('shows suggestions when typing a matching substring', () => {
    render(
      <Autocomplete value="Los" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    expect(screen.getByText('Los Angeles')).toBeInTheDocument();
  });

  it('filters suggestions case-insensitively', () => {
    render(
      <Autocomplete value="san" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    expect(screen.getByText('San Bernardino')).toBeInTheDocument();
    expect(screen.getByText('San Francisco')).toBeInTheDocument();
    expect(screen.queryByText('Los Angeles')).not.toBeInTheDocument();
  });

  it('does not show listbox when no options match', () => {
    render(
      <Autocomplete value="xyz" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('calls onChange when typing', () => {
    render(
      <Autocomplete value="" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.change(input, { target: { value: 'Or' } });
    expect(onChange).toHaveBeenCalledWith('Or');
  });

  it('calls onChange when selecting an option by click', () => {
    render(
      <Autocomplete value="Or" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    fireEvent.mouseDown(screen.getByText('Orange'));
    expect(onChange).toHaveBeenCalledWith('Orange');
  });

  it('navigates suggestions with ArrowDown and selects with Enter', () => {
    render(
      <Autocomplete value="San" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);

    // ArrowDown to first item
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    const listbox = screen.getByRole('listbox');
    const firstOption = listbox.querySelector('[aria-selected="true"]');
    expect(firstOption).toHaveTextContent('San Bernardino');

    // ArrowDown to second item
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    const secondOption = listbox.querySelector('[aria-selected="true"]');
    expect(secondOption).toHaveTextContent('San Francisco');

    // Enter to select
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('San Francisco');
  });

  it('navigates suggestions with ArrowUp', () => {
    render(
      <Autocomplete value="San" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);

    // ArrowDown to first, then ArrowUp wraps to last
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    const listbox = screen.getByRole('listbox');
    const selected = listbox.querySelector('[aria-selected="true"]');
    expect(selected).toHaveTextContent('San Francisco');
  });

  it('closes suggestions on Escape', () => {
    render(
      <Autocomplete value="Los" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    expect(screen.getByRole('listbox')).toBeInTheDocument();

    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('sets aria-expanded based on suggestion visibility', () => {
    render(
      <Autocomplete value="Los" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');

    // Before focus, not expanded
    expect(input).toHaveAttribute('aria-expanded', 'false');

    fireEvent.focus(input);
    expect(input).toHaveAttribute('aria-expanded', 'true');
  });

  it('accepts an aria-label prop', () => {
    render(
      <Autocomplete
        value=""
        onChange={onChange}
        options={OPTIONS}
        aria-label="County"
      />,
    );
    expect(screen.getByRole('combobox')).toHaveAttribute('aria-label', 'County');
  });

  it('accepts a custom id', () => {
    render(
      <Autocomplete
        value=""
        onChange={onChange}
        options={OPTIONS}
        id="my-input"
      />,
    );
    expect(screen.getByRole('combobox')).toHaveAttribute('id', 'my-input');
  });

  it('shows all options when input is empty and focused', () => {
    render(
      <Autocomplete value="" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    const listbox = screen.getByRole('listbox');
    expect(listbox.children).toHaveLength(OPTIONS.length);
  });

  it('allows free-text input that does not match any option', () => {
    render(
      <Autocomplete value="Custom text" onChange={onChange} options={OPTIONS} />,
    );
    const input = screen.getByRole('combobox');
    expect(input).toHaveValue('Custom text');
    // No listbox because nothing matches
    fireEvent.focus(input);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });
});
