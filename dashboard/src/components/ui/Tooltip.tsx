import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  Fragment,
  type FocusEvent,
  type MouseEvent,
  type PointerEvent,
  type ReactElement,
  type ReactNode,
} from 'react';
import { BUBBLE_CLASSES, BUBBLE_WIDTH, isKeyboardFocus, useFloating, type FloatingAlign, type FloatingSide } from './floating';

interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  /** Wrap long text in a fixed-width bubble instead of one line. */
  multiline?: boolean;
  /** Which side of the trigger the bubble prefers (it flips when there is no room). */
  side?: FloatingSide;
  /** Horizontal alignment against the trigger. */
  align?: FloatingAlign;
  /** Also toggle on click/tap, for touch screens where there is no hover. */
  toggleOnClick?: boolean;
  /** Point the trigger's aria-describedby at the bubble while it is shown.
   *  Turn off when the content only repeats the trigger's accessible name. */
  describe?: boolean;
}

type TriggerProps = {
  onPointerDown?: (e: PointerEvent<HTMLElement>) => void;
  onMouseEnter?: (e: MouseEvent<HTMLElement>) => void;
  onMouseLeave?: (e: MouseEvent<HTMLElement>) => void;
  onFocus?: (e: FocusEvent<HTMLElement>) => void;
  onBlur?: (e: FocusEvent<HTMLElement>) => void;
  onClick?: (e: MouseEvent<HTMLElement>) => void;
  'aria-describedby'?: string;
};

/**
 * Hover/focus tooltip on the overlay surface. Opens 400 ms after a mouse
 * hover and at once on keyboard focus; Escape closes it without moving focus
 * (WCAG 1.4.13). Touch input never opens it on hover; pass `toggleOnClick`
 * where a tap should show it.
 *
 * When `children` is a single element the handlers are attached to it
 * directly (no wrapper element, so flex layouts are unchanged); otherwise the
 * children are wrapped in an inline span.
 */
export function Tooltip({
  content,
  children,
  multiline = false,
  side = 'top',
  align = 'center',
  toggleOnClick = false,
  describe = true,
}: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // The element the bubble is placed against, taken from the event that
  // opens it (state, not a ref, so render never reads a ref).
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const bubbleRef = useRef<HTMLSpanElement | null>(null);
  const id = useId();
  const style = useFloating(visible, anchor, bubbleRef, side, align);

  // Empty content: the tooltip never shows, but the tree stays the same, so a
  // caller can switch the content on and off without remounting its trigger.
  const enabled = content !== null && content !== undefined && content !== '' && content !== false;

  const showSoon = useCallback(() => {
    clearTimeout(timerRef.current);
    if (!enabled) return;
    timerRef.current = setTimeout(() => setVisible(true), 400);
  }, [enabled]);
  const showNow = useCallback(() => {
    clearTimeout(timerRef.current);
    if (!enabled) return;
    setVisible(true);
  }, [enabled]);
  // True while a click/tap holds the tooltip open. A click also focuses the
  // trigger, so "toggle" must not undo what focus or hover just opened.
  const pinnedRef = useRef(false);
  const hide = useCallback(() => {
    clearTimeout(timerRef.current);
    pinnedRef.current = false;
    setVisible(false);
  }, []);
  const toggle = useCallback(() => {
    clearTimeout(timerRef.current);
    if (pinnedRef.current || !enabled) {
      pinnedRef.current = false;
      setVisible(false);
    } else {
      pinnedRef.current = true;
      setVisible(true);
    }
  }, [enabled]);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  // Escape dismisses a shown tooltip without moving focus or hover. Claimed
  // on window in the capture phase, before a Dialog or Drawer underneath
  // treats the key as "close".
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

  // A tap elsewhere closes a tooltip that a tap opened.
  useEffect(() => {
    if (!visible || !toggleOnClick) return;
    function onDown(e: Event) {
      if (anchor && anchor.contains(e.target as Node)) return;
      hide();
    }
    document.addEventListener('pointerdown', onDown, true);
    return () => document.removeEventListener('pointerdown', onDown, true);
  }, [visible, toggleOnClick, hide, anchor]);

  // Browsers fire compatibility mouseenter events after a tap; a recent
  // touch means the hover isn't real.
  const touchAt = useRef(0);
  const onPointerDown = useCallback((e: PointerEvent<HTMLElement>) => {
    if (e.pointerType === 'touch' || e.pointerType === 'pen') touchAt.current = e.timeStamp || 1;
  }, []);
  const onMouseEnter = useCallback(
    (e: MouseEvent<HTMLElement>) => {
      if (!touchAt.current || e.timeStamp - touchAt.current > 1000) showSoon();
    },
    [showSoon],
  );
  // Only a keyboard focus opens it; a click focuses the trigger too, and the
  // pointer already decided.
  const onFocus = useCallback(
    (e: FocusEvent<HTMLElement>) => {
      if (isKeyboardFocus(e.target as HTMLElement)) showNow();
    },
    [showNow],
  );

  const bubble = visible && enabled ? (
    <span
      ref={bubbleRef}
      id={id}
      role="tooltip"
      style={{ ...style, boxShadow: 'var(--shadow-md)' }}
      className={`${BUBBLE_CLASSES} pointer-events-none ${
        multiline ? `w-max ${BUBBLE_WIDTH} whitespace-pre-line` : 'whitespace-nowrap'
      }`}
    >
      {content}
    </span>
  ) : null;

  const single = isValidElement(children) && children.type !== Fragment;

  if (single) {
    const child = children as ReactElement<TriggerProps>;
    const p = child.props;
    const describedBy =
      describe && visible ? [p['aria-describedby'], id].filter(Boolean).join(' ') : p['aria-describedby'];
    // The trigger keeps its own ref; see `anchor` above. The handlers below
    // read refs only when their events fire, never during render; the
    // compiler can't see that through cloneElement.
    // eslint-disable-next-line react-hooks/refs
    const cloned = cloneElement(child, {
      onPointerDown: (e: PointerEvent<HTMLElement>) => {
        p.onPointerDown?.(e);
        onPointerDown(e);
      },
      onMouseEnter: (e: MouseEvent<HTMLElement>) => {
        p.onMouseEnter?.(e);
        setAnchor(e.currentTarget);
        onMouseEnter(e);
      },
      onMouseLeave: (e: MouseEvent<HTMLElement>) => {
        p.onMouseLeave?.(e);
        hide();
      },
      onFocus: (e: FocusEvent<HTMLElement>) => {
        p.onFocus?.(e);
        setAnchor(e.currentTarget);
        onFocus(e);
      },
      onBlur: (e: FocusEvent<HTMLElement>) => {
        p.onBlur?.(e);
        hide();
      },
      onClick: (e: MouseEvent<HTMLElement>) => {
        p.onClick?.(e);
        setAnchor(e.currentTarget);
        if (toggleOnClick) toggle();
        else hide();
      },
      'aria-describedby': describedBy || undefined,
    });
    return (
      <>
        {cloned}
        {bubble}
      </>
    );
  }

  return (
    <span
      ref={setAnchor}
      className="relative inline-flex"
      onPointerDown={onPointerDown}
      onMouseEnter={onMouseEnter}
      onMouseLeave={hide}
      onFocus={onFocus}
      onBlur={hide}
      onClick={toggleOnClick ? toggle : undefined}
    >
      {children}
      {bubble}
    </span>
  );
}
