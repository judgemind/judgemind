import type { Metadata } from 'next';
import './globals.css';
import { Header } from '@/components/layout/Header';
import { ApolloProvider } from '@/providers/ApolloProvider';
import { AuthProvider } from '@/providers/AuthProvider';
import { ThemeProvider } from '@/providers/ThemeProvider';

export const metadata: Metadata = {
  title: 'Judgemind — Legal Research Platform',
  description: 'Free, open-source California court ruling research and judge analytics.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Theme-color meta for browser address bar / tab tinting */}
        <meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)" />
        <meta name="theme-color" content="#0a0a0a" media="(prefers-color-scheme: dark)" />
        {/* Set theme + color-scheme before first paint to avoid flash */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              try {
                var t = localStorage.getItem('theme');
                var p = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
                var resolved = t || p;
                if (resolved === 'dark') {
                  document.documentElement.classList.add('dark');
                  document.documentElement.style.colorScheme = 'dark';
                } else {
                  document.documentElement.style.colorScheme = 'light';
                }
              } catch(e) {}
            `,
          }}
        />
      </head>
      <body>
        {/* Skip link for keyboard/screen-reader users (WCAG 2.1 Level A) */}
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[100] focus:rounded focus:bg-background focus:px-4 focus:py-2 focus:text-foreground focus:ring-2 focus:ring-ring"
        >
          Skip to main content
        </a>
        <ThemeProvider>
          <ApolloProvider>
            <AuthProvider>
              <div className="flex min-h-screen flex-col">
                <Header />
                {children}
              </div>
            </AuthProvider>
          </ApolloProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
