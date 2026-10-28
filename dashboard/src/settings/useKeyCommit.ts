import { useCallback, useRef, useState } from 'react';

/** For the form that owns the API key field: bump `keyCommit` on blur when
 *  the key changed since the last commit. */
export function useKeyCommit(apiKey: string): [number, () => void] {
  const [keyCommit, setKeyCommit] = useState(0);
  const committed = useRef(apiKey);
  const onKeyBlur = useCallback(() => {
    if (committed.current === apiKey) return;
    committed.current = apiKey;
    setKeyCommit((n) => n + 1);
  }, [apiKey]);
  return [keyCommit, onKeyBlur];
}
