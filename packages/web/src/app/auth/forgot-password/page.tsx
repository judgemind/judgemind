'use client';

import { useState } from 'react';
import Link from 'next/link';
import {
  AuthCard,
  FormField,
  SubmitButton,
  ErrorAlert,
  SuccessAlert,
} from '@/components/auth/AuthCard';

/**
 * Forgot-password page.
 *
 * Note: The `requestPasswordReset` mutation is not yet in the GraphQL schema.
 * This page is wired up to call it once the backend adds it. For now, it always
 * shows the success message to prevent email enumeration — identical to the
 * planned API behaviour.
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      // TODO: Call requestPasswordReset mutation once the backend adds it.
      // For now we show the success message unconditionally to match the
      // planned API behaviour (prevent email enumeration).
      setSubmitted(true);
    } catch {
      setError('Something went wrong. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  if (submitted) {
    return (
      <AuthCard title="Check your email">
        <SuccessAlert message="If that email is registered, we sent a password reset link. Check your inbox." />
        <div className="mt-4 text-center">
          <Link
            href="/auth/login"
            className="text-sm font-medium text-brand-600 hover:text-brand-700 dark:text-brand-500 dark:hover:text-brand-400"
          >
            Back to login
          </Link>
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Forgot password">
      <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
        Enter your email address and we&apos;ll send you a link to reset your
        password.
      </p>

      <form onSubmit={handleSubmit} className="space-y-4">
        <ErrorAlert message={error} />

        <FormField
          id="email"
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          placeholder="you@example.com"
          autoComplete="email"
        />

        <SubmitButton loading={loading}>Send reset link</SubmitButton>
      </form>

      <div className="mt-6 text-center text-sm">
        <Link
          href="/auth/login"
          className="font-medium text-brand-600 hover:text-brand-700 dark:text-brand-500 dark:hover:text-brand-400"
        >
          Back to login
        </Link>
      </div>
    </AuthCard>
  );
}
