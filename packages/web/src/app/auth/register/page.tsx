'use client';

import { useState } from 'react';
import { useMutation } from '@apollo/client';
import Link from 'next/link';
import { REGISTER_MUTATION } from '@/lib/auth-mutations';
import type { AuthPayload } from '@/lib/auth-mutations';
import { useAuth } from '@/providers/AuthProvider';
import {
  AuthCard,
  FormField,
  SubmitButton,
  ErrorAlert,
  SuccessAlert,
} from '@/components/auth/AuthCard';

export default function RegisterPage() {
  const { setUser } = useAuth();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const [registerMutation, { loading }] = useMutation<{
    register: AuthPayload;
  }>(REGISTER_MUTATION);

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

    try {
      const { data } = await registerMutation({
        variables: { email, password },
      });
      if (data?.register.user) {
        setUser(data.register.user);
        setSuccess(true);
      }
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : 'Registration failed. Please try again.';
      setError(message);
    }
  }

  if (success) {
    return (
      <AuthCard title="Check your email">
        <SuccessAlert message="Your account has been created. Please check your email to verify your account before logging in." />
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
    <AuthCard title="Create an account">
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

        <FormField
          id="password"
          label="Password"
          type="password"
          value={password}
          onChange={setPassword}
          placeholder="At least 8 characters"
          autoComplete="new-password"
          minLength={8}
        />

        <FormField
          id="confirm-password"
          label="Confirm password"
          type="password"
          value={confirmPassword}
          onChange={setConfirmPassword}
          placeholder="Re-enter your password"
          autoComplete="new-password"
        />

        <SubmitButton loading={loading}>Create account</SubmitButton>
      </form>

      <div className="mt-6 text-center text-sm text-slate-500 dark:text-slate-400">
        Already have an account?{' '}
        <Link
          href="/auth/login"
          className="font-medium text-brand-600 hover:text-brand-700 dark:text-brand-500 dark:hover:text-brand-400"
        >
          Log in
        </Link>
      </div>
    </AuthCard>
  );
}
