import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';

import HelperPanel from './HelperPanel';

/** The scope a helper turn is opened with (project page or session detail). */
export interface HelperScope {
  project_id?: string;
  session_ids?: string[];
  /** Display name for the scope chip (project name / session title). Never sent to the API. */
  label?: string;
}

interface HelperContextValue {
  /**
   * Open the helper drawer. Passing a scope starts a new conversation bound
   * to that project/session; omitting it starts an unscoped conversation.
   * Undefined when rendered outside a HelperProvider; callers use that to
   * hide the "Ask the helper" entry points.
   */
  openHelper?: (scope?: HelperScope) => void;
}

const HelperContext = createContext<HelperContextValue | null>(null);

export function HelperProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<HelperScope | undefined>(undefined);
  // The element that had focus when the drawer opened, so focus returns to it
  // when the drawer closes instead of dropping to <body>.
  const triggerRef = useRef<HTMLElement | null>(null);

  const openHelper = useCallback((next?: HelperScope) => {
    triggerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setScope(next);
    setOpen(true);
  }, []);

  const closeHelper = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  const value = useMemo(() => ({ openHelper }), [openHelper]);

  return (
    <HelperContext.Provider value={value}>
      {children}
      <HelperPanel open={open} onClose={closeHelper} scope={scope} />
    </HelperContext.Provider>
  );
}

export function useHelper(): HelperContextValue {
  const ctx = useContext(HelperContext);
  // No provider means the drawer can't open; return a safe default so a screen
  // rendered in isolation (e.g. a focused unit test) just hides its helper
  // entry points instead of crashing.
  if (!ctx) return { openHelper: undefined };
  return ctx;
}
