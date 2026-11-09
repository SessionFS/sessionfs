import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

export type FloatingSide = 'top' | 'bottom';
export type FloatingAlign = 'center' | 'start' | 'end';

const GAP = 6;
const EDGE = 8;

/**
 * Places a bubble next to its trigger with `position: fixed`, so a parent
 * with `overflow: hidden` (tables, cards, drawers) can't clip it, and keeps it
 * inside the viewport: it flips to the other side when there is no room and
 * slides sideways instead of running off the screen edge.
 *
 * The bubble is rendered in place (not in a portal) so tab order and a
 * dialog's focus trap still see it. A transformed ancestor turns `fixed` into
 * "relative to that ancestor"; the second pass measures where the bubble
 * really landed and corrects by the difference.
 */
export function useFloating(
  open: boolean,
  anchor: RefObject<HTMLElement | null> | HTMLElement | null,
  bubbleRef: RefObject<HTMLElement | null>,
  side: FloatingSide = 'top',
  align: FloatingAlign = 'center',
): CSSProperties {
  const [style, setStyle] = useState<CSSProperties>({ position: 'fixed', top: 0, left: 0, visibility: 'hidden' });

  useLayoutEffect(() => {
    if (!open) return;
    function place() {
      const trigger = anchor && 'current' in anchor ? anchor.current : anchor;
      const bubble = bubbleRef.current;
      if (!trigger || !bubble) return;
      const t = trigger.getBoundingClientRect();
      const vw = window.innerWidth || document.documentElement.clientWidth;
      const vh = window.innerHeight || document.documentElement.clientHeight;
      const w = bubble.offsetWidth;
      const h = bubble.offsetHeight;

      let left =
        align === 'start' ? t.left : align === 'end' ? t.right - w : t.left + t.width / 2 - w / 2;
      left = Math.max(EDGE, Math.min(left, vw - w - EDGE));

      const above = t.top - h - GAP;
      const below = t.bottom + GAP;
      let top = side === 'top' ? above : below;
      if (side === 'top' && above < EDGE && below + h <= vh - EDGE) top = below;
      if (side === 'bottom' && below + h > vh - EDGE && above >= EDGE) top = above;

      // First pass at the wanted coordinates, then correct for a containing
      // block that isn't the viewport.
      bubble.style.top = `${top}px`;
      bubble.style.left = `${left}px`;
      const landed = bubble.getBoundingClientRect();
      const dx = landed.left - left;
      const dy = landed.top - top;
      setStyle({ position: 'fixed', top: top - dy, left: left - dx, visibility: 'visible' });
    }
    place();
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [open, anchor, bubbleRef, side, align]);

  return open ? style : { position: 'fixed', top: 0, left: 0, visibility: 'hidden' };
}

/** Shared bubble look: overlay surface, 12px text, at most ~280px wide. */
export const BUBBLE_CLASSES =
  'z-[60] rounded border border-border bg-overlay px-2.5 py-1.5 text-xs leading-snug text-text-primary text-left font-normal normal-case tracking-normal';

export const BUBBLE_WIDTH = 'max-w-[min(17.5rem,calc(100vw-1rem))]';

/** True when focus came from the keyboard (or the browser can't tell). */
export function isKeyboardFocus(el: Element): boolean {
  try {
    return el.matches(':focus-visible');
  } catch {
    return true;
  }
}
