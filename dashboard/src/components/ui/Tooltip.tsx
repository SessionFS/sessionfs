import { useState, useRef, useCallback, useEffect, type ReactNode } from 'react';

interface TooltipProps {
  content: string;
  children: ReactNode;
  /** Wrap long text in a fixed-width bubble instead of one line. */
  multiline?: boolean;
  /** Which side of the trigger the bubble opens on. */
  side?: 'top' | 'bottom';
  /** Horizontal alignment against the trigger (start keeps a bubble next to
   *  a label from running off the left edge). */
  align?: 'center' | 'start' | 'end';
  /** Also toggle on click/tap, for touch screens where there is no hover. */
  toggleOnClick?: boolean;
}

const SIDE = {
  top: 'bottom-full mb-1.5',
  bottom: 'top-full mt-1.5',
} as const;

const ALIGN = {
  center: 'left-1/2 -translate-x-1/2',
  start: 'left-0',
  end: 'right-0',
} as const;

/** Simple hover tooltip — 400ms delay, --overlay surface, 12px. */
export function Tooltip({
  content,
  children,
  multiline = false,
  side = 'top',
  align = 'center',
  toggleOnClick = false,
}: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const show = useCallback(() => {
    timerRef.current = setTimeout(() => setVisible(true), 400);
  }, []);

  const hide = useCallback(() => {
    clearTimeout(timerRef.current);
    setVisible(false);
  }, []);

  // Escape dismisses a shown tooltip without moving focus or hover
  // (WCAG 1.4.13). Claimed on window in the capture phase, before a Dialog
  // or Drawer underneath treats the key as "close".
  useEffect(() => {
    if (!visible) return;
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      hide();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [visible, hide]);

  const toggle = useCallback(() => {
    clearTimeout(timerRef.current);
    setVisible((v) => !v);
  }, []);

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      onClick={toggleOnClick ? toggle : undefined}
    >
      {children}
      {visible && (
        <span
          role="tooltip"
          className={`absolute ${SIDE[side]} ${ALIGN[align]} px-2 py-1 rounded text-xs leading-tight z-50 pointer-events-none border border-border bg-overlay text-text-primary ${
            multiline ? 'w-72 max-w-[min(18rem,calc(100vw-3rem))] whitespace-pre-line text-left leading-snug py-1.5 font-normal' : 'whitespace-nowrap'
          }`}
          style={{ boxShadow: 'var(--shadow-md)' }}
        >
          {content}
        </span>
      )}
    </span>
  );
}
