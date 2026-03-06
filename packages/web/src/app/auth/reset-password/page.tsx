'use client';

import { Suspense, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';
import {
  AuthCard,
  FormField,
  SubmitButton,
  ErrorAlert,
  SuccessAlert,
} from '@/components/auth/AuthCard';

/**
 * Reset-password inner component.
 *
 * Note: The `resetPassword` mutation is not yet in the GraphQL schema.
 * This page is wired up to call it once the backend adds it. For now it
 * validates the form and shows a placeholder error.
 */
function ResetPasswordContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get('token');

  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);

  if (!token) {
    return (
      <AuthCard title="Reset password">
        <ErrorAlert message="No reset token provided. Please use the link from your email." />
        <div className="mt-4 text-center">
          <Link
            href="/auth/forgot-password"
            className="text-sm font-medium text-brand-600 hover:text-brand-700 dark:text-brand-500 dark:hover:text-brand-400"
          >
            Request a new link
          </Link>
        </div>
      </AuthCard>
    );
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    if (password.length < 8) {
      setError('Password must be at least 8 characters.');
      return;
    }

    setLoading(true);

    try {
      // TODO: Call resetPassword mutation once the backend adds it.
      // resetPassword(token, newPassword) -> boolean
      setError(
        'Password reset is not yet available. The backend mutation has not been implemented.',
      );
    } catch {
      setError('Reset link is expired or invalid. Please request a new one.');
    } finally {
      setLoading(false);
    }
  }

  if (success) {
    return (
      <AuthCard title="Password reset">
        <SuccessAlert message="Your password has been reset. You can now log in with your new password." />
        <div className="mt-4 text-center">
          <Link
            href="/auth/login"
            className="text-sm font-medium text-brand-600 hover:text-brand-700 dark:text-brand-500 dark:hover:text-brand-400"
          >
            Go to login
          </Link>
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Set a new password">
      <form onSubmit={handleSubmit} className="space-y-4">
        <ErrorAlert message={error} />

        <FormField
          id="password"
          label="New password"
          type="password"
          value={password}
          onChange={setPassword}
          placeholder="At least 8 characters"
          autoComplete="new-password"
          minLength={8}
        />

        <FormField
          id="confirm-password"
          label="Confirm new password"
          type="password"
          value={confirmPassword}
          onChange={setConfirmPassword}
          placeholder="Re-enter your new password"
          autoComplete="new-password"
        />

        <SubmitButton loading={loading}>Reset password</SubmitButton>
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

export default function ResetPasswordPage() {
  return (
    <Suspense
      fallback={
        <AuthCard title="Reset password">
          <p className="text-center text-sm text-slate-500 dark:text-slate-400">
            Loading...
          </p>
        </AuthCard>
      }
    >
      <ResetPasswordContent />
    </Suspense>
  );
}
