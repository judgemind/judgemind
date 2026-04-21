-- Up Migration
--
-- Dispatcher v2 — scope-miss follow-up to migration 35's Backfill 2.
--
-- Problem
-- -------
-- Migration 35 (issue #2959) backfilled rows with ``status='failed'`` +
-- ``failure_summary='dispatcher restarted'`` + ``pr_number IS NOT NULL``
-- by flipping them to ``status='succeeded'`` and stamping
-- ``merged_at = ended_at``. The fix was correct but the WHERE clause was
-- too narrow: it only matched ``status='failed'``. At least one row
-- (issue #2902) had been terminalized with ``status='plan_blocked'``
-- instead — same root cause (daemon restart between merge and the
-- terminal write), same observable symptom (red ✗ in the admin panel
-- for work that actually shipped). See issues #2953, #2959, #2902,
-- #2943 for full context.
--
-- Fix
-- ---
-- One additional UPDATE, structurally identical to migration 35's
-- Backfill 2 but matching ``status='plan_blocked'`` instead of
-- ``status='failed'``. Same idempotency guard (``merged_at IS NULL``),
-- same columns updated (``merged_at``, ``status``), same rationale: the
-- row merged before the daemon restart; the stored ``plan_blocked`` was
-- the bug.

UPDATE dispatcher.agents
   SET merged_at = ended_at,
       status    = 'succeeded'
 WHERE pr_number IS NOT NULL
   AND status = 'plan_blocked'
   AND failure_summary = 'dispatcher restarted'
   AND ended_at IS NOT NULL
   AND merged_at IS NULL;


-- Down Migration
--
-- This migration contains no DDL — there are no columns to drop.
-- The status flip above is NOT reversed on down-migrate for the same
-- reason stated in migration 35's down section: we have no safe way to
-- identify which ``succeeded`` rows came from this flip vs. the normal
-- success path without re-reading ``failure_summary``, and
-- ``succeeded`` was always the correct terminal value for these rows
-- anyway. Re-flipping to ``plan_blocked`` would re-introduce the
-- false-red glyph for work that demonstrably shipped.
