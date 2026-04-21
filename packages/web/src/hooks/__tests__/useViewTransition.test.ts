/**
 * Tests for {@link useViewTransitionUpdate} (#2967).
 *
 * Covers the four branches of {@link shouldSkipViewTransition}:
 *   1. `document.startViewTransition` undefined → skip (hard cut).
 *   2. `prefers-reduced-motion: reduce` matches → skip.
 *   3. API present + motion allowed → wrap in
 *      `startViewTransition(() => flushSync(fn))`.
 *   4. `matchMedia` throws → defensive fall-through to wrap.
 */

import { describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import {
  shouldSkipViewTransition,
  useViewTransitionUpdate,
} from '../useViewTransition';

describe('shouldSkipViewTransition', () => {
  it('returns true when document is undefined (SSR / worker context)', () => {
    expect(shouldSkipViewTransition(undefined, () => ({ matches: false }))).toBe(
      true,
    );
  });

  it('returns true when startViewTransition is not a function (Firefox today)', () => {
    expect(
      shouldSkipViewTransition({}, () => ({ matches: false })),
    ).toBe(true);
  });

  it('returns true when prefers-reduced-motion: reduce matches', () => {
    const doc = { startViewTransition: vi.fn() };
    const matchMedia = vi.fn().mockReturnValue({ matches: true });
    expect(shouldSkipViewTransition(doc, matchMedia)).toBe(true);
    expect(matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)');
  });

  it('returns false when the API is present and motion is allowed', () => {
    const doc = { startViewTransition: vi.fn() };
    const matchMedia = vi.fn().mockReturnValue({ matches: false });
    expect(shouldSkipViewTransition(doc, matchMedia)).toBe(false);
  });

  it('returns false (defensive fall-through) when matchMedia throws', () => {
    const doc = { startViewTransition: vi.fn() };
    const matchMedia = vi.fn().mockImplementation(() => {
      throw new Error('boom');
    });
    expect(shouldSkipViewTransition(doc, matchMedia)).toBe(false);
  });

  it('returns false when matchMedia is undefined (rare but possible)', () => {
    const doc = { startViewTransition: vi.fn() };
    expect(shouldSkipViewTransition(doc, undefined)).toBe(false);
  });
});

describe('useViewTransitionUpdate', () => {
  it('wraps the update in startViewTransition when the API is available', () => {
    const capture: Array<() => void> = [];
    const startViewTransition = vi.fn((cb: () => void) => {
      capture.push(cb);
      return {};
    });
    const documentOverride = { startViewTransition };
    const matchMediaOverride = vi.fn().mockReturnValue({ matches: false });

    const { result } = renderHook(() =>
      useViewTransitionUpdate({ documentOverride, matchMediaOverride }),
    );

    const updateFn = vi.fn();
    result.current(updateFn);

    // API was called …
    expect(startViewTransition).toHaveBeenCalledTimes(1);
    // … but the update hasn't fired yet — it runs inside the callback.
    expect(updateFn).not.toHaveBeenCalled();

    // Simulate the browser invoking the update callback.
    capture[0]();
    // Now the state update ran (flushSync called it synchronously).
    expect(updateFn).toHaveBeenCalledTimes(1);
  });

  it('bypasses startViewTransition when the API is undefined', () => {
    const documentOverride = {};
    const matchMediaOverride = vi.fn().mockReturnValue({ matches: false });

    const { result } = renderHook(() =>
      useViewTransitionUpdate({ documentOverride, matchMediaOverride }),
    );

    const updateFn = vi.fn();
    result.current(updateFn);

    // The update ran directly — no wrapper.
    expect(updateFn).toHaveBeenCalledTimes(1);
  });

  it('bypasses startViewTransition when prefers-reduced-motion: reduce', () => {
    const startViewTransition = vi.fn();
    const documentOverride = { startViewTransition };
    const matchMediaOverride = vi.fn().mockReturnValue({ matches: true });

    const { result } = renderHook(() =>
      useViewTransitionUpdate({ documentOverride, matchMediaOverride }),
    );

    const updateFn = vi.fn();
    result.current(updateFn);

    expect(startViewTransition).not.toHaveBeenCalled();
    expect(updateFn).toHaveBeenCalledTimes(1);
  });

  it('returns a stable callback across renders when options are unchanged', () => {
    const documentOverride = {};
    const matchMediaOverride = () => ({ matches: false });

    const { result, rerender } = renderHook(() =>
      useViewTransitionUpdate({ documentOverride, matchMediaOverride }),
    );

    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});
