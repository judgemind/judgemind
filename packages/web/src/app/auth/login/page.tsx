'use client';

import { useState } from 'react';
import { useMutation } from '@apollo/client';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { LOGIN_MUTATION } from '@/lib/auth-mutations';
import type { AuthPayload } from '@/lib/auth-mutations';
import { useAuth } from '@/providers/AuthProvider';
import {
  AuthCard,
  FormField,
  SubmitButton,
  ErrorAlert,
} from '@/components/auth/AuthCard';

export default function LoginPage() {
  const router = useRouter();
  const { setUser } = useAuth();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);

  const [loginMutation, { loading }] = useMutation<{ login: AuthPayload }>(
    LOGIN_MUTATION,
  );

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    try {
      const { data } = await loginMutation({
        variables: { email, password },
      });
      if (data?.login.user) {
        setUser(data.login.user);
        router.push('/');
      }
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Login failed. Please try again.';
      setError(message);
    }
  }

  return (
    <AuthCard title="Log in to your account">
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
          placeholder="Enter your password"
          autoComplete="current-password"
        />

        <SubmitButton loading={loading}>Log in</SubmitButton>
      </form>

      <div className="mt-6 space-y-2 text-center text-sm">
        <p className="text-slate-500 dark:text-slate-400">
          Don&apos;t have an account?{' '}
          <Link
            href="/auth/register"
            className="font-medium text-brand-600 hover:text-brand-700 dark:text-brand-500 dark:hover:text-brand-400"
          >
            Register
          </Link>
        </p>
        <p>
          <Link
            href="/auth/forgot-password"
            className="font-medium text-brand-600 hover:text-brand-700 dark:text-brand-500 dark:hover:text-brand-400"
          >
            Forgot password?
          </Link>
        </p>
      </div>
    </AuthCard>
  );
}
