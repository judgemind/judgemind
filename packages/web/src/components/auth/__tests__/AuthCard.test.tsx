import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Mock next/link as a simple anchor
vi.mock('next/link', () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
    [key: string]: unknown;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import {
  AuthCard,
  FormField,
  SubmitButton,
  ErrorAlert,
  SuccessAlert,
} from '../AuthCard';

// ---------------------------------------------------------------------------
// AuthCard
// ---------------------------------------------------------------------------

describe('AuthCard', () => {
  it('renders the title', () => {
    render(<AuthCard title="Test Title">Content</AuthCard>);
    expect(screen.getByText('Test Title')).toBeInTheDocument();
  });

  it('renders children', () => {
    render(
      <AuthCard title="Title">
        <span data-testid="child">Hello</span>
      </AuthCard>,
    );
    expect(screen.getByTestId('child')).toBeInTheDocument();
  });

  it('renders the wordmark logo link', () => {
    render(<AuthCard title="Title">Content</AuthCard>);
    expect(screen.getByText('judge')).toBeInTheDocument();
    expect(screen.getByText('mind')).toBeInTheDocument();
    expect(screen.getByText('judge').closest('a')).toHaveAttribute('href', '/');
  });
});

// ---------------------------------------------------------------------------
// FormField
// ---------------------------------------------------------------------------

describe('FormField', () => {
  it('renders a label and input', () => {
    render(
      <FormField
        id="email"
        label="Email"
        type="email"
        value=""
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });

  it('calls onChange when the user types', () => {
    const onChange = vi.fn();
    render(
      <FormField
        id="name"
        label="Name"
        type="text"
        value=""
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText('Name'), {
      target: { value: 'Alice' },
    });
    expect(onChange).toHaveBeenCalledWith('Alice');
  });

  it('is required by default', () => {
    render(
      <FormField
        id="field"
        label="Field"
        type="text"
        value=""
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Field')).toBeRequired();
  });

  it('respects required=false', () => {
    render(
      <FormField
        id="field"
        label="Field"
        type="text"
        value=""
        onChange={vi.fn()}
        required={false}
      />,
    );
    expect(screen.getByLabelText('Field')).not.toBeRequired();
  });

  it('sets placeholder and autoComplete', () => {
    render(
      <FormField
        id="email"
        label="Email"
        type="email"
        value=""
        onChange={vi.fn()}
        placeholder="you@example.com"
        autoComplete="email"
      />,
    );
    const input = screen.getByLabelText('Email');
    expect(input).toHaveAttribute('placeholder', 'you@example.com');
    expect(input).toHaveAttribute('autocomplete', 'email');
  });

  it('sets minLength when provided', () => {
    render(
      <FormField
        id="pw"
        label="Password"
        type="password"
        value=""
        onChange={vi.fn()}
        minLength={8}
      />,
    );
    expect(screen.getByLabelText('Password')).toHaveAttribute(
      'minlength',
      '8',
    );
  });
});

// ---------------------------------------------------------------------------
// SubmitButton
// ---------------------------------------------------------------------------

describe('SubmitButton', () => {
  it('renders children when not loading', () => {
    render(<SubmitButton loading={false}>Log in</SubmitButton>);
    expect(screen.getByRole('button')).toHaveTextContent('Log in');
  });

  it('shows "Please wait\u2026" when loading', () => {
    render(<SubmitButton loading={true}>Log in</SubmitButton>);
    expect(screen.getByRole('button')).toHaveTextContent('Please wait\u2026');
  });

  it('is disabled when loading', () => {
    render(<SubmitButton loading={true}>Log in</SubmitButton>);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('is enabled when not loading', () => {
    render(<SubmitButton loading={false}>Log in</SubmitButton>);
    expect(screen.getByRole('button')).toBeEnabled();
  });
});

// ---------------------------------------------------------------------------
// ErrorAlert
// ---------------------------------------------------------------------------

describe('ErrorAlert', () => {
  it('renders the error message', () => {
    render(<ErrorAlert message="Something went wrong" />);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Something went wrong',
    );
  });

  it('renders nothing when message is null', () => {
    const { container } = render(<ErrorAlert message={null} />);
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// SuccessAlert
// ---------------------------------------------------------------------------

describe('SuccessAlert', () => {
  it('renders the success message', () => {
    render(<SuccessAlert message="Account created!" />);
    expect(screen.getByRole('status')).toHaveTextContent('Account created!');
  });

  it('renders nothing when message is null', () => {
    const { container } = render(<SuccessAlert message={null} />);
    expect(container.firstChild).toBeNull();
  });
});
