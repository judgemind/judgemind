'use client';

import { SECTION_HEADING } from '@/lib/typography';

interface ConfigPanelProps {
  configKeys: ReadonlyArray<{ key: string; label: string }>;
  spawnFrozenUntil: string | null;
}

/**
 * Read-only config panel for Phase 1. The `concurrency_cap`, `idle_mode`,
 * and `backoff_seconds` keys live in `dispatcher.config`; Phase 1's
 * GraphQL surface does not yet expose per-key values except
 * `spawnFrozenUntil`. Editing is a Phase 2 follow-up.
 */
export function ConfigPanel({ configKeys, spawnFrozenUntil }: ConfigPanelProps) {
  return (
    <section
      aria-labelledby="config-heading"
      className="rounded-lg border border-border bg-card p-4"
    >
      <h2 id="config-heading" className={SECTION_HEADING}>
        Config
      </h2>
      <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        {configKeys.map((item) => (
          <div key={item.key} className="flex flex-col">
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              {item.label}
            </dt>
            <dd className="mt-1 font-mono text-sm text-foreground">
              seeded (see <code>dispatcher.config</code>)
            </dd>
          </div>
        ))}
        <div className="flex flex-col">
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">
            Spawn frozen until
          </dt>
          <dd className="mt-1 font-mono text-sm text-foreground">
            {spawnFrozenUntil ?? '—'}
          </dd>
        </div>
      </dl>
      <p className="mt-3 text-xs text-muted-foreground">
        Read-only in Phase 1. Inline editing ships with the Phase 2 follow-up.
      </p>
    </section>
  );
}
