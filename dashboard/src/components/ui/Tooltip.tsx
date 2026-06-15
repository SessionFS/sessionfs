import { useState, useRef, useCallback, type ReactNode } from 'react';

interface TooltipProps {
  content: string;
  children: ReactNode;
}

/** Simple hover tooltip — 400ms delay, --overlay surface, 12px. */
export function Tooltip({ content, children }: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const show = useCallback(() => {
    timerRef.current = setTimeout(() => setVisible(true), 400);
  }, []);

  const hide = useCallback(() => {
    clearTimeout(timerRef.current);
    setVisible(false);
  }, []);

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {visible && (
        <span
          role="tooltip"
          className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 rounded text-xs leading-tight whitespace-nowrap z-50 pointer-events-none border border-border bg-overlay text-text-primary"
          style={{ boxShadow: 'var(--shadow-md)' }}
        >
          {content}
        </span>
      )}
    </span>
  );
}
