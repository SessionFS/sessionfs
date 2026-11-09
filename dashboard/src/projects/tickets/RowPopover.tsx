import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject } from 'react';

/* ── Row popover ──
 *
 * The small anchored surface behind a row's status and assignee buttons.
 * `kind="menu"` holds a WAI-ARIA menu the caller renders (`role="menu"`,
 * named by `label`; arrow keys move between items, focus starts on the
 * first one), plus any note under it; `kind="dialog"` is a non-modal panel that holds
 * a form (the reassign editor). Escape and an outside click close it and
 * hand focus back to the trigger. While open, the cell is lifted above the
 * rows after it so they can't paint over the panel. */

export interface PopoverTriggerProps {
  ref: RefObject<HTMLButtonElement | null>;
  'aria-haspopup': 'menu' | 'dialog';
  'aria-expanded': boolean;
  'aria-controls': string | undefined;
  onClick: () => void;
}

export function RowPopover({
  kind,
  label,
  align = 'left',
  side = 'bottom',
  trigger,
  children,
  className = '',
  panelClassName = '',
  open: openProp,
  onOpenChange,
}: {
  kind: 'menu' | 'dialog';
  /** Accessible name of the menu or panel. */
  label: string;
  align?: 'left' | 'right';
  /** Open below the trigger (default) or above it (the bulk bar at the
   *  bottom of the screen). */
  side?: 'bottom' | 'top';
  trigger: (props: PopoverTriggerProps) => ReactNode;
  children: (close: (restoreFocus?: boolean) => void) => ReactNode;
  className?: string;
  panelClassName?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [openLocal, setOpenLocal] = useState(false);
  const open = openProp ?? openLocal;
  const setOpen = useCallback(
    (next: boolean) => {
      if (onOpenChange) onOpenChange(next);
      else setOpenLocal(next);
    },
    [onOpenChange],
  );
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  // Focus goes back to the trigger after the panel is gone (an effect, so
  // the close callback handed to the content never touches a ref).
  const [restoreTick, setRestoreTick] = useState(0);
  const close = useCallback(
    (restoreFocus = true) => {
      setOpen(false);
      if (restoreFocus) setRestoreTick((n) => n + 1);
    },
    [setOpen],
  );
  useEffect(() => {
    if (restoreTick) triggerRef.current?.focus();
  }, [restoreTick]);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) close(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      if (!wrapperRef.current?.contains(e.target as Node) && e.target !== document.body) return;
      e.preventDefault();
      close(true);
    }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, close]);

  // A menu starts on its first item; a dialog lets its content place focus.
  useEffect(() => {
    if (!open || kind !== 'menu') return;
    panelRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus();
  }, [open, kind]);

  function onMenuKey(e: ReactKeyboardEvent) {
    if (kind !== 'menu') return;
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return;
    const items = Array.from(
      panelRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? [],
    );
    if (!items.length) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLElement);
    let next = 0;
    if (e.key === 'ArrowDown') next = at < 0 ? 0 : (at + 1) % items.length;
    else if (e.key === 'ArrowUp') next = at <= 0 ? items.length - 1 : at - 1;
    else if (e.key === 'End') next = items.length - 1;
    items[next].focus();
  }

  return (
    <span ref={wrapperRef} className={`tk-pop ${className}`} data-open={open || undefined}>
      {trigger({
        ref: triggerRef,
        'aria-haspopup': kind,
        'aria-expanded': open,
        'aria-controls': open ? panelId : undefined,
        onClick: () => setOpen(!open),
      })}
      {open && (
        <div
          ref={panelRef}
          id={panelId}
          role={kind === 'dialog' ? 'dialog' : undefined}
          aria-label={kind === 'dialog' ? label : undefined}
          onKeyDown={onMenuKey}
          className={`tk-pop-panel ${align === 'right' ? 'tk-pop-right' : 'tk-pop-left'}${side === 'top' ? ' tk-pop-up' : ''} ${panelClassName}`}
        >
          {children(close)}
        </div>
      )}
    </span>
  );
}
