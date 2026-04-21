'use client';

import { useCallback } from 'react';
import { flushSync } from 'react-dom';

/**
 * Subset of the DOM View Transitions API we call into. Kept minimal
 * because we never read back from the returned `ViewTransition` —
 * starting the transition is fire-and-forget.
 *
 * The spec surface:
 * https://developer.mozilla.org/en-US/docs/Web/API/View_Transition_API
 */
type StartViewTransition = (updateCallback: () => void) => unknown;

/**
 * Tests and SSR paths can pass a synthetic `document`-like object. Real
 * browser code uses the ambient DOM `document`.
 */
interface ViewTransitionDocument {
  startViewTransition?: StartViewTransition;
}

/**
 * Window-ish object that exposes `matchMedia`. In browsers `window` has
 * this; in jsdom it's present but doesn't actually resolve the query.
 * Tests swap in a stub via the `matchMediaOverride` option.
 */
interface ViewTransitionWindow {
  matchMedia?: (query: string) => { matches: boolean };
}

/**
 * Options for {@link useViewTransitionUpdate}. All fields are optional
 * overrides intended for tests — production code passes nothing.
 */
export interface UseViewTransitionOptions {
  /** Stub out `document.startViewTransition` for deterministic tests. */
  documentOverride?: ViewTransitionDocument;
  /** Stub out `window.matchMedia` for deterministic tests. */
  matchMediaOverride?: (query: string) => { matches: boolean };
}

/**
 * Decide whether to skip the view-transition wrapper for a given
 * environment. The View Transitions API itself honours
 * ``prefers-reduced-motion`` for its built-in cross-fade defaults, but
 * that protection only covers the *unnamed* groups — every Magic Move
 * in this dashboard uses an explicit ``view-transition-name`` and so
 * needs the caller to short-circuit. We do that by skipping
 * ``startViewTransition`` entirely when:
 *
 *   1. ``document.startViewTransition`` is undefined (Firefox today, plus
 *      any browser that predates Chromium 111 / Safari 18), OR
 *   2. ``prefers-reduced-motion: reduce`` is set.
 *
 * Both branches fall through to a plain synchronous update, which the
 * issue body accepts as the correct behaviour. See #2967.
 */
export function shouldSkipViewTransition(
  doc: ViewTransitionDocument | undefined,
  matchMedia: ((query: string) => { matches: boolean }) | undefined,
): boolean {
  if (!doc || typeof doc.startViewTransition !== 'function') {
    return true;
  }
  if (typeof matchMedia === 'function') {
    try {
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
        return true;
      }
    } catch {
      // Defensive: a broken `matchMedia` must not block state updates.
      // Fall through and run the transition.
    }
  }
  return false;
}

/**
 * Return a ``startViewTransitionUpdate`` callback that wraps a React
 * state mutation in ``document.startViewTransition(() =>
 * flushSync(updateFn))``.
 *
 * `flushSync` is required: the View Transitions API snapshots the DOM
 * synchronously inside its callback, and React's automatic batching
 * would otherwise defer the update past the snapshot boundary — the
 * transition would then resolve with "nothing changed" and fall back to
 * a hard cut.
 *
 * When the API is unavailable, or when the user has expressed
 * ``prefers-reduced-motion: reduce``, the returned callback runs the
 * update synchronously with no view-transition wrapper. See
 * {@link shouldSkipViewTransition} for the decision logic.
 *
 * Issue #2967 — Magic Move animations on /admin/dispatcher.
 */
export function useViewTransitionUpdate(
  options: UseViewTransitionOptions = {},
): (updateFn: () => void) => void {
  return useCallback(
    (updateFn: () => void) => {
      const doc: ViewTransitionDocument | undefined =
        options.documentOverride ??
        (typeof document === 'undefined' ? undefined : document);
      const matchMedia =
        options.matchMediaOverride ??
        (typeof window === 'undefined'
          ? undefined
          : (window as ViewTransitionWindow).matchMedia?.bind(window));

      if (shouldSkipViewTransition(doc, matchMedia)) {
        updateFn();
        return;
      }

      // The `!` is safe — `shouldSkipViewTransition` returned false, so
      // `doc.startViewTransition` is a function.
      doc!.startViewTransition!(() => {
        flushSync(updateFn);
      });
    },
    [options.documentOverride, options.matchMediaOverride],
  );
}
