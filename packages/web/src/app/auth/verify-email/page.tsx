'use client';

import { Suspense, useEffect, useState } from 'react';
import { useMutation } from '@apollo/client';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { VERIFY_EMAIL_MUTATION } from '@/lib/auth-mutations';
import {
  AuthCard,
  ErrorAlert,
  SuccessAlert,
} from '@/components/auth/AuthCard';

function VerifyEmailContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get('token');

  const [status, setStatus] = useState<'loading' | 'success' | 'error'>(
    'loading',
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [verifyEmailMutation] = useMutation<{ verifyEmail: boolean }>(
    VERIFY_EMAIL_MUTATION,
  );

  useEffect(() => {
    if (!token) {
      setStatus('error');
      setErrorMessage('No verification token provided.');
      return;
    }

    let cancelled = false;

    async function verify() {
      try {
        const { data } = await verifyEmailMutation({
          variables: { token },
        });
        if (cancelled) return;
        if (data?.verifyEmail) {
          setStatus('success');
        } else {
          setStatus('error');
          setErrorMessage('Verification link is expired or invalid.');
        }
      } catch (err: unknown) {
        if (cancelled) return;
        setStatus('error');
        const message =
          err instanceof Error
            ? err.message
            : 'Verification link is expired or invalid.';
        setErrorMessage(message);
      }
    }

    void verify();

    return () => {
      cancelled = true;
    };
  }, [token, verifyEmailMutation]);

  return (
    <AuthCard title="Email verification">
      {status === 'loading' && (
        <p className="text-center text-sm text-slate-500 dark:text-slate-400">
          {'Verifying your email\u2026'}
        </p>
      )}

      {status === 'success' && (
        <>
          <SuccessAlert message="Email verified — you can now log in." />
          <div className="mt-4 text-center">
            <Link
              href="/auth/login"
              className="text-sm font-medium text-brand-accent hover:text-brand-accent-hover dark:text-brand-accent-light dark:hover:text-brand-accent-lighter"
            >
              Go to login
            </Link>
          </div>
        </>
      )}

      {status === 'error' && (
        <>
          <ErrorAlert message={errorMessage} />
          <div className="mt-4 text-center">
            <Link
              href="/auth/login"
              className="text-sm font-medium text-brand-accent hover:text-brand-accent-hover dark:text-brand-accent-light dark:hover:text-brand-accent-lighter"
            >
              Go to login
            </Link>
          </div>
        </>
      )}
    </AuthCard>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense
      fallback={
        <AuthCard title="Email verification">
          <p className="text-center text-sm text-slate-500 dark:text-slate-400">
            {'Loading\u2026'}
          </p>
        </AuthCard>
      }
    >
      <VerifyEmailContent />
    </Suspense>
  );
}
