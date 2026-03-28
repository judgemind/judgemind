'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery, gql } from '@apollo/client';
import { Search } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Wordmark } from '@/components/Wordmark';
import { PAGE_TITLE, SECTION_HEADING } from '@/lib/typography';

const PLATFORM_STATS_QUERY = gql`
  query PlatformStats {
    platformStats {
      countiesCount
      rulingsCount
      judgesCount
    }
  }
`;

interface StatsData {
  platformStats: {
    countiesCount: number;
    rulingsCount: number;
    judgesCount: number;
  };
}

function formatStatNumber(n: number): string {
  if (n >= 1000) {
    return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  }
  return n.toLocaleString();
}

function StatsBar() {
  const { data, loading } = useQuery<StatsData>(PLATFORM_STATS_QUERY);

  return (
    <div className="grid grid-cols-3 gap-4" data-testid="stats-bar">
      {loading || !data ? (
        <>
          <Card>
            <CardContent className="p-4 text-center">
              <Skeleton className="mx-auto mb-1 h-7 w-12" />
              <Skeleton className="mx-auto h-4 w-20" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <Skeleton className="mx-auto mb-1 h-7 w-12" />
              <Skeleton className="mx-auto h-4 w-20" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <Skeleton className="mx-auto mb-1 h-7 w-12" />
              <Skeleton className="mx-auto h-4 w-20" />
            </CardContent>
          </Card>
        </>
      ) : (
        <>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-bold text-foreground">
                {formatStatNumber(data.platformStats.countiesCount)}
              </p>
              <p className="text-sm text-muted-foreground">Counties covered</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-bold text-foreground">
                {formatStatNumber(data.platformStats.rulingsCount)}
              </p>
              <p className="text-sm text-muted-foreground">Rulings captured</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-bold text-foreground">
                {formatStatNumber(data.platformStats.judgesCount)}
              </p>
              <p className="text-sm text-muted-foreground">Judges tracked</p>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

const HOW_IT_WORKS_STEPS = [
  {
    step: '1',
    title: 'We capture tentative rulings daily',
    description:
      'Automated scrapers collect tentative rulings from California Superior Courts before they expire.',
  },
  {
    step: '2',
    title: 'Search by keyword, judge, or case',
    description:
      'Full-text search across all captured rulings. Filter by county, judge, date, motion type, or outcome.',
  },
  {
    step: '3',
    title: 'Track judge analytics over time',
    description:
      'See grant/deny rates by motion type for any judge. Understand tendencies before your next hearing.',
  },
] as const;

function HowItWorks() {
  return (
    <section data-testid="how-it-works">
      <h2 className={`mb-3 ${SECTION_HEADING}`}>How it works</h2>
      <div className="grid gap-4 sm:grid-cols-3">
        {HOW_IT_WORKS_STEPS.map((item) => (
          <Card key={item.step}>
            <CardContent className="p-4">
              <p className="mb-1 text-sm font-semibold text-primary">{`Step ${item.step}`}</p>
              <p className="font-medium text-foreground">{item.title}</p>
              <p className="mt-1 text-sm text-muted-foreground">{item.description}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}

function HeroSearch() {
  const router = useRouter();
  const [query, setQuery] = useState('');

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) return;
    const params = new URLSearchParams();
    params.set('q', trimmed);
    router.push(`/search?${params.toString()}`);
  }

  return (
    <form onSubmit={handleSubmit} data-testid="hero-search">
      <div className="relative">
        <Search
          className="absolute left-4 top-1/2 h-5 w-5 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          type="search"
          name="q"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by keyword, case number, judge, or party\u2026"
          className="h-12 pl-11 pr-24 text-base"
          aria-label="Search rulings"
        />
        <Button
          type="submit"
          size="sm"
          className="absolute right-2 top-1/2 -translate-y-1/2"
        >
          Search
        </Button>
      </div>
    </form>
  );
}

export function HomeContent() {
  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div>
        <h1 className={PAGE_TITLE}>
          Free legal research for everyone
        </h1>
        <p className="mt-4 text-lg text-muted-foreground">
          <Wordmark size="sm" className="font-normal" /> captures California tentative rulings and judicial analytics — open source,
          free forever.
        </p>
      </div>

      <HeroSearch />

      <div className="flex gap-3">
        <Button variant="outline" asChild>
          <Link href="/search">Advanced search</Link>
        </Button>
        <Button variant="outline" asChild>
          <Link href="/rulings">Latest rulings</Link>
        </Button>
      </div>

      <StatsBar />

      <HowItWorks />
    </div>
  );
}
