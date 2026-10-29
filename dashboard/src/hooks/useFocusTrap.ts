import { useEffect, useRef, type RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/**
 * Traps keyboard focus within a dialog element.
 *
 * On mount: saves the previously focused element, then focuses the first
 * focusable element inside the container — or the container itself (give it
 * tabIndex={-1}) when nothing inside can take focus.
 * On Tab / Shift+Tab: wraps focus so it never leaves the container. When
 * every control is disabled (a write in flight) focus stays on the panel, and
 * when focus has dropped onto <body> (a focused button was disabled), Tab
 * brings it back to the first control or the panel instead of the page.
 * On unmount: restores focus to the previously focused element.
 */
/**
 * Active traps, in activation order. A dialog opened from a drawer leaves the
 * drawer's trap active underneath; only the topmost live one may rescue a
 * stray Tab, or the drawer would pull focus out from behind the dialog.
 */
const activeTraps: HTMLElement[] = [];

/**
 * The topmost live trap: skip disconnected and inert ones, and any that
 * contains another live trap (a dialog rendered inside a drawer activates
 * first, since child effects run before the parent's, yet sits above it);
 * of the rest, the most recently activated.
 */
function topTrap(): HTMLElement | null {
  const live = activeTraps.filter((t) => t.isConnected && !t.closest('[inert]'));
  const leaves = live.filter((t) => !live.some((o) => o !== t && t.contains(o)));
  return leaves.length ? leaves[leaves.length - 1] : null;
}

export function useFocusTrap(containerRef: RefObject<HTMLElement | null>) {
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // Save the element that had focus before the dialog opened.
    previouslyFocused.current = document.activeElement as HTMLElement | null;

    const fallback = () => {
      if (container.hasAttribute('tabindex')) container.focus();
    };

    // Focus the first focusable element inside the dialog.
    const focusable = container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
    if (focusable.length > 0) {
      focusable[0].focus();
    } else {
      fallback();
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;

      const elements = container!.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
      if (elements.length === 0) {
        // Every control is disabled: keep focus on the panel.
        e.preventDefault();
        fallback();
        return;
      }

      const first = elements[0];
      const last = elements[elements.length - 1];

      if (e.shiftKey) {
        // Shift+Tab: if focus is on the first element (or the panel), wrap to last.
        if (document.activeElement === first || document.activeElement === container) {
          e.preventDefault();
          last.focus();
        }
      } else {
        // Tab: if focus is on the last element, wrap to first.
        if (document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }

    // Focus that has fallen out of the container onto <body>, or rests on a
    // button disabled while focused (a write in flight), no longer sends key
    // events through the container, so Tab is caught on the document too.
    function handleStrayTab(e: KeyboardEvent) {
      if (e.key !== 'Tab' || e.defaultPrevented) return;
      const active = document.activeElement as (HTMLElement & { disabled?: boolean }) | null;
      // Focus is fine where it is unless it fell onto <body>, left the page,
      // or sits on a control of ours that has just been disabled (which no
      // longer receives key events in some browsers).
      const stray =
        !active ||
        active === document.body ||
        !active.isConnected ||
        (active.disabled === true && container!.contains(active));
      if (!stray) return;
      // Only the newest live trap answers (not one under a dialog opened
      // over it, nor one made inert).
      if (topTrap() !== container) return;
      e.preventDefault();
      const elements = container!.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
      if (elements.length > 0) (e.shiftKey ? elements[elements.length - 1] : elements[0]).focus();
      else fallback();
    }

    activeTraps.push(container);
    container.addEventListener('keydown', handleKeyDown);
    document.addEventListener('keydown', handleStrayTab);

    const saved = previouslyFocused.current;
    return () => {
      const at = activeTraps.lastIndexOf(container);
      if (at >= 0) activeTraps.splice(at, 1);
      container.removeEventListener('keydown', handleKeyDown);
      document.removeEventListener('keydown', handleStrayTab);
      // Restore focus to the element that was focused before the dialog.
      saved?.focus();
    };
  }, [containerRef]);
}
