# Database Schema Quick Reference

Quick-reference for agents writing SQL scripts. **Always check this before using
`ON CONFLICT`** — the target must match an existing UNIQUE constraint.

## Unique Constraints by Table

| Table | Unique constraint columns | Source |
|---|---|---|
| `courts` | `(id)` PK | schema.sql |
| `courts` | `(court_code)` | schema.sql |
| `judges` | `(id)` PK | schema.sql |
| `judges` | `(canonical_name, court_id)` | migration 10 |
| `judge_aliases` | `(id)` PK | schema.sql |
| `attorneys` | `(id)` PK | schema.sql |
| `attorney_aliases` | `(id)` PK | schema.sql |
| `parties` | `(id)` PK | schema.sql |
| `party_aliases` | `(id)` PK | schema.sql |
| `cases` | `(id)` PK | schema.sql |
| `cases` | `(court_id, case_number)` | schema.sql |
| `case_judges` | `(case_id, judge_id)` PK | schema.sql |
| `case_attorneys` | `(id)` PK | schema.sql |
| `case_attorneys` | `(case_id, attorney_id, role)` | migration 8 |
| `case_parties` | `(id)` PK | schema.sql |
| `case_parties` | `(case_id, party_id, role)` | migration 7 |
| `documents` | `(id)` PK | schema.sql |
| `rulings` | `(id)` PK | schema.sql |
| `rulings` | `(document_id)` | migration 3 |
| `rulings` | `(case_id, ruling_text_hash)` partial | migration 11 |
| `users` | `(id)` PK | schema.sql |
| `users` | `(email)` | schema.sql |
| `users` | `(google_id)` | migration 2 |
| `users` | `(api_key)` | schema.sql |
| `refresh_tokens` | `(id)` PK | schema.sql |
| `refresh_tokens` | `(token_hash)` | migration 2 |
| `alert_subscriptions` | `(id)` PK | schema.sql |
| `alert_events` | `(id)` PK | schema.sql |
| `staging.captures` | `(id)` PK | schema.sql |
| `staging.ruled_items` | `(id)` PK | schema.sql |
| `court_directory_snapshots` | `(id)` PK | schema.sql |
| `scraper_runs` | `(id)` PK | schema.sql |
| `data_quality_metrics` | `(id)` PK | schema.sql |

## Common ON CONFLICT patterns

### Valid patterns

```sql
-- Courts: upsert by court_code
INSERT INTO courts (...) VALUES (...)
ON CONFLICT (court_code) DO UPDATE SET ...

-- Cases: upsert by natural key
INSERT INTO cases (...) VALUES (...)
ON CONFLICT (court_id, case_number) DO UPDATE SET ...

-- Judges: upsert by canonical name + court
INSERT INTO judges (...) VALUES (...)
ON CONFLICT (canonical_name, court_id) DO UPDATE SET ...

-- Documents: upsert by deterministic UUID
INSERT INTO documents (...) VALUES (...)
ON CONFLICT (id) DO UPDATE SET ...

-- Rulings: upsert by document
INSERT INTO rulings (...) VALUES (...)
ON CONFLICT (document_id) DO UPDATE SET ...

-- Join tables: idempotent insert
INSERT INTO case_parties (...) VALUES (...)
ON CONFLICT (case_id, party_id, role) DO NOTHING

INSERT INTO case_judges (...) VALUES (...)
ON CONFLICT (case_id, judge_id) DO NOTHING
```

### Invalid patterns (will fail at runtime)

```sql
-- WRONG: parties has no UNIQUE on canonical_name alone
INSERT INTO parties (canonical_name) VALUES (...)
ON CONFLICT (canonical_name) DO UPDATE ...

-- WRONG: party_aliases has no non-PK UNIQUE constraint
INSERT INTO party_aliases (...) VALUES (...)
ON CONFLICT (party_id, raw_name) DO NOTHING

-- WRONG: case_parties UNIQUE is (case_id, party_id, role), not (case_id, party_id)
INSERT INTO case_parties (...) VALUES (...)
ON CONFLICT (case_id, party_id) DO UPDATE ...
```

## Automated check

CI runs `scripts/check-sql-conflicts.py` which validates all `ON CONFLICT`
targets against the `UNIQUE_CONSTRAINTS` map. If a migration adds or removes
a constraint, update the map in the script.

## Keeping this document in sync

When you add a migration that creates or drops a UNIQUE constraint:

1. Update `scripts/check-sql-conflicts.py` — add or remove the constraint
   in the `UNIQUE_CONSTRAINTS` dict.
2. Update this table above.
3. If the constraint should also be in `schema.sql` for local dev, update
   that file too (and the `check-schema-drift.sh` check will enforce it).
