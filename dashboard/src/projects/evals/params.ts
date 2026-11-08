import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';

/**
 * The Evals and Artifacts views keep what is open in the URL (next to the
 * project's ?tab=&sub=), so a refresh or a shared link lands on the same run,
 * suite or artifact. Updates replace the history entry and keep other params.
 */
export function useViewParams() {
  const [params, setParams] = useSearchParams();
  const set = useCallback(
    (next: Record<string, string | null>) => {
      setParams(
        (prev) => {
          const p = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(next)) {
            if (v === null || v === '') p.delete(k);
            else p.set(k, v);
          }
          return p;
        },
        { replace: true },
      );
    },
    [setParams],
  );
  return { get: (k: string) => params.get(k), set };
}
