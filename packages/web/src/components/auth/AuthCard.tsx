'use client';

import Link from 'next/link';

interface AuthCardProps {
  title: string;
  children: React.ReactNode;
}

/**
 * Shared wrapper for auth pages — centered card with the Judgemind logo.
 */
export function AuthCard({ title, children }: AuthCardProps) {
  return (
    <div className="flex min-h-[70vh] items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <Link
            href="/"
            className="text-2xl font-bold text-brand-700 dark:text-brand-500"
          >
            Judgemind
          </Link>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-8 shadow-sm dark:border-slate-700 dark:bg-slate-800/50">
          <h1 className="mb-6 text-xl font-semibold text-slate-900 dark:text-slate-100">
            {title}
          </h1>
          {children}
        </div>
      </div>
    </div>
  );
}

interface FormFieldProps {
  id: string;
  label: string;
  type: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  autoComplete?: string;
  required?: boolean;
  minLength?: number;
}

export function FormField({
  id,
  label,
  type,
  value,
  onChange,
  placeholder,
  autoComplete,
  required = true,
  minLength,
}: FormFieldProps) {
  return (
    <div>
      <label
        htmlFor={id}
        className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300"
      >
        {label}
      </label>
      <input
        id={id}
        name={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={autoComplete}
        required={required}
        minLength={minLength}
        className="w-full rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
      />
    </div>
  );
}

interface SubmitButtonProps {
  loading: boolean;
  children: React.ReactNode;
}

export function SubmitButton({ loading, children }: SubmitButtonProps) {
  return (
    <button
      type="submit"
      disabled={loading}
      className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-2 disabled:opacity-50 dark:focus:ring-offset-slate-800"
    >
      {loading ? 'Please wait...' : children}
    </button>
  );
}

interface ErrorAlertProps {
  message: string | null;
}

export function ErrorAlert({ message }: ErrorAlertProps) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400"
    >
      {message}
    </div>
  );
}

interface SuccessAlertProps {
  message: string | null;
}

export function SuccessAlert({ message }: SuccessAlertProps) {
  if (!message) return null;
  return (
    <div
      role="status"
      className="rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700 dark:border-green-800 dark:bg-green-900/20 dark:text-green-400"
    >
      {message}
    </div>
  );
}
