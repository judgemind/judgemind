-- Up Migration
--
-- Dispatcher v2 — per-phase token usage + cost metering (#2869).
--
-- Context
-- -------
-- Before this migration, ``dispatcher.phase_outputs`` had no metering
-- columns, so per-PR / per-phase token + cost telemetry was unavailable.
-- The Max plan's fixed monthly fee masks per-run cost until the
-- extra-usage threshold is hit, so operators couldn't answer
-- questions like "is ralph-on-sonnet cheaper than ralph-on-haiku for
-- scraper fixes?" or "which phase is the cost bottleneck?" without
-- a data source. Tokens are meterable independently of billing, and
-- Claude Code emits them via ``claude -p --output-format json`` as a
-- ``{usage: {...}, total_cost_usd: ...}`` block on stdout.
--
-- Design notes
-- ------------
-- - All columns are nullable — historical rows inserted before this
--   migration have no usage data to back-fill, and phases that crash
--   before ``claude`` prints its JSON envelope legitimately have no
--   usage block (NULL keeps "no signal" distinguishable from zero).
-- - ``bigint`` for token counts — input + output + cache counts can
--   easily exceed 2^31 for long ralph iterations. A single ralph run
--   commonly exceeds 10M input tokens after cache reads; the JSON
--   envelope sums all turns. ``int`` would silently overflow.
-- - ``numeric(10,4)`` for ``cost_usd`` — Anthropic's list-price rates
--   produce costs with up to ~4 decimal places of significant digits
--   ($0.0001 per 1k tokens of Haiku input). ``numeric`` avoids floating
--   rounding drift across SUM aggregations (the documented "last 10 PR
--   cost" query).
-- - ``text`` for ``model_used`` — we store the resolved model name
--   (e.g. ``"sonnet"``, ``"claude-sonnet-4-5"``) rather than an enum, so
--   adding a new model never requires a migration.
-- - No index — the documented dashboard queries aggregate by
--   ``(agent_id)`` or ``(phase)`` which are already covered by
--   existing indexes (``idx_dispatcher_phase_outputs_agent_phase_attempt``
--   from migration 30 + the primary key). Adding a ``(cost_usd)`` index
--   would slow every INSERT for rarely-executed "top N most expensive
--   phases" queries; the full table is small enough (30-day retention,
--   housekeeping tick at migration 24) that a seq scan is cheap.
--
-- Known caveat
-- ------------
-- ``total_cost_usd`` from Claude Code is the list-price cost estimated
-- against posted Anthropic rates — it does NOT reflect Max plan
-- discounts. Comparable run-to-run (tuning useful) but NOT actual
-- billed spend. Documented in ``docs/agent/infrastructure-reference.md``.

ALTER TABLE dispatcher.phase_outputs
    ADD COLUMN tokens_input        bigint,
    ADD COLUMN tokens_output       bigint,
    ADD COLUMN tokens_cache_read   bigint,
    ADD COLUMN tokens_cache_write  bigint,
    ADD COLUMN cost_usd            numeric(10, 4),
    ADD COLUMN model_used          text;

COMMENT ON COLUMN dispatcher.phase_outputs.tokens_input IS
    'Input tokens consumed by the phase (sum across all claude API turns). '
    'Parsed from the ``usage.input_tokens`` field of the ``claude -p --output-format json`` envelope. '
    'Nullable — historical rows and pre-JSON-envelope failures have no signal. Issue #2869.';

COMMENT ON COLUMN dispatcher.phase_outputs.tokens_output IS
    'Output tokens emitted by the phase. Parsed from ``usage.output_tokens``. '
    'Nullable. Issue #2869.';

COMMENT ON COLUMN dispatcher.phase_outputs.tokens_cache_read IS
    'Cache-read tokens (cheap tokens served from prompt-cache). Parsed from '
    '``usage.cache_read_input_tokens``. Nullable. Issue #2869.';

COMMENT ON COLUMN dispatcher.phase_outputs.tokens_cache_write IS
    'Cache-creation tokens (prompt written to cache for reuse). Parsed from '
    '``usage.cache_creation_input_tokens``. Nullable. Issue #2869.';

COMMENT ON COLUMN dispatcher.phase_outputs.cost_usd IS
    'Claude Code''s list-price cost estimate for the phase. Parsed from '
    '``total_cost_usd``. NOT Max-plan-adjusted — comparable run-to-run but '
    'not actual billed spend. Nullable. Issue #2869.';

COMMENT ON COLUMN dispatcher.phase_outputs.model_used IS
    'The resolved Claude model the phase ran on (e.g. ``"sonnet"``, '
    '``"claude-sonnet-4-5"``). Free-form text — adding a new model never '
    'requires a migration. Nullable. Issue #2869.';


-- Down Migration
ALTER TABLE dispatcher.phase_outputs
    DROP COLUMN IF EXISTS tokens_input,
    DROP COLUMN IF EXISTS tokens_output,
    DROP COLUMN IF EXISTS tokens_cache_read,
    DROP COLUMN IF EXISTS tokens_cache_write,
    DROP COLUMN IF EXISTS cost_usd,
    DROP COLUMN IF EXISTS model_used;
