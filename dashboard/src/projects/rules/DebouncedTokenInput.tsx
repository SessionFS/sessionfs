import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Input } from '../../components/ui';

interface DebouncedTokenInputProps {
  value: number;
  disabled: boolean;
  ariaLabel: string;
  /**
   * Commit a confirmed value. Returns a Promise resolving to `true`
   * when the server-side mutation succeeds, `false` when it fails
   * (the failure has already been surfaced to the user via a toast,
   * so the caller just needs to know whether to proceed with a
   * follow-up action). May also return `void` for callers that don't
   * round-trip to a server.
   */
  onCommit: (n: number) => Promise<boolean> | void;
}

export interface DebouncedTokenInputHandle {
  /**
   * Force-commit any pending draft immediately, cancelling the timer.
   * Returns `Promise<true>` if nothing was pending OR the commit
   * succeeded; `Promise<false>` if the commit failed. Compile and
   * other gated follow-ups should treat a `false` resolution as
   * "skip — the rules update did not land".
   */
  flush: () => Promise<boolean>;
}

// Local-draft + 600ms debounce around `<input type="number">`. Each
// keystroke previously fired patchRules → PUT /rules with an ETag,
// so typing "8000" produced four sequential mutations: the first
// succeeded with a new etag and the next three 409'd with stale
// etag, surfaced as a cascade of "refresh and try again" toasts.
// The debounce coalesces typing into one mutation per pause.
//
// Flush points (added post-Codex round-1 review on this fix):
//   - onBlur:  commit immediately when focus leaves the input
//   - unmount: commit pending change on tab-switch / navigation
//   - parent:  flush() exposed via ref so Compile can drain pending
//              edits before invoking the compile mutation
export const DebouncedTokenInput = forwardRef<DebouncedTokenInputHandle, DebouncedTokenInputProps>(
  function DebouncedTokenInput({ value, disabled, ariaLabel, onCommit }, ref) {
    const [draft, setDraft] = useState<string>(String(value));
    const lastCommittedRef = useRef<number>(value);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const pendingRef = useRef<number | null>(null);

    // Tracks the most recent in-flight onCommit promise so `flush()`
    // can resolve only when the backend has actually accepted the
    // value — not just when the local mutate() call was issued. This
    // is what lets Compile await a freshly-typed value's PUT to land
    // before its POST goes out, instead of racing it on HTTP/2.
    //
    // Resolves to `true` on success, `false` on failure (the caller
    // gates follow-up actions on this). `Promise.resolve(true)` is
    // the initial value so flush() with nothing pending is a no-op.
    const lastCommitPromiseRef = useRef<Promise<boolean>>(Promise.resolve(true));

    // `onCommit` may be an inline arrow at the parent and so change
    // each render. Capture the latest in a ref so the unmount-only
    // effect can call it without resubscribing the cleanup.
    const onCommitRef = useRef(onCommit);
    useEffect(() => {
      onCommitRef.current = onCommit;
    }, [onCommit]);

    function clearTimer() {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    }

    function commit(n: number) {
      pendingRef.current = null;
      lastCommittedRef.current = n;
      const result = onCommitRef.current(n);
      // Normalize to Promise<boolean>:
      //  - synchronous void return → success (true)
      //  - resolved with `false` → failure (callers will short-circuit)
      //  - rejected (defensive — onCommit shouldn't throw, but a buggy
      //    callback shouldn't crash the dashboard) → treated as failure
      lastCommitPromiseRef.current = Promise.resolve(result).then(
        (ok) => ok !== false,
        () => false,
      );
    }

    useImperativeHandle(
      ref,
      () => ({
        flush() {
          clearTimer();
          if (pendingRef.current !== null) {
            commit(pendingRef.current);
          }
          return lastCommitPromiseRef.current;
        },
      }),
      [],
    );

    // Re-sync local draft when the server-confirmed value changes
    // (e.g. another tab edited the rules, or a successful commit
    // rounds the value). Skip when the draft already matches to
    // avoid disrupting in-progress typing.
    useEffect(() => {
      if (value !== lastCommittedRef.current) {
        lastCommittedRef.current = value;
        setDraft(String(value));
        pendingRef.current = null;
        clearTimer();
      }
    }, [value]);

    // Debounce: schedule a commit 600ms after the user stops typing.
    useEffect(() => {
      const n = parseInt(draft, 10);
      // v0.10.24 tk_d4a13a68b6724ba6 — pinning the max here too
      // (not just on the input attribute) because users can paste
      // values that the up/down arrows would block. Server cap is
      // 20000; we mirror that here so the debounced PUT never fires
      // an over-cap request → no opaque 422 toast.
      if (
        Number.isNaN(n)
        || n < 0
        || n > 20000
        || n === lastCommittedRef.current
      ) {
        pendingRef.current = null;
        return;
      }
      pendingRef.current = n;
      clearTimer();
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        if (pendingRef.current !== null) {
          commit(pendingRef.current);
        }
      }, 600);
      return clearTimer;
    }, [draft]);

    // Unmount-only flush. Empty deps so this cleanup runs only when
    // the component leaves the tree (tab switch, navigation, etc.) —
    // not on every re-render. Uses onCommitRef.current to stay
    // current without depending on `onCommit`.
    useEffect(() => {
      return () => {
        if (pendingRef.current !== null) {
          onCommitRef.current(pendingRef.current);
        }
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    return (
      <Input
        type="number"
        min={0}
        max={20000}
        step={500}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          clearTimer();
          if (pendingRef.current !== null) {
            commit(pendingRef.current);
          }
        }}
        disabled={disabled}
        aria-label={ariaLabel}
        className="w-24 text-xs px-2 py-1 rounded"
      />
    );
  },
);
