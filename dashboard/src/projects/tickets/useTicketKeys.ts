import { useEffect, useRef, type RefObject } from 'react';

/* ── Tickets keyboard ──
 *
 * Single-key shortcuts for the Tickets tab (the help dialog lists them).
 * They only fire while focus is on the tab (or nowhere), never while typing
 * in a field, and never while a menu, listbox or dialog is open, except the
 * ticket slide-over itself: from inside it the keys that act on the open
 * ticket (status, assign, tabs, next / previous) still work. Modifier
 * chords (⌘K and the like) are left alone. */

export type TicketKeyAction =
  | { type: 'move'; delta: 1 | -1 }
  | { type: 'open' }
  | { type: 'escape' }
  | { type: 'toggleSelect' }
  | { type: 'status' }
  | { type: 'assign' }
  | { type: 'new' }
  | { type: 'search' }
  | { type: 'toggleLayout' }
  | { type: 'view'; index: number }
  | { type: 'paneTab'; delta: 1 | -1 }
  | { type: 'help' };

export interface KeyLike {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
}

/** The action for a key press, or null for keys the tab doesn't use. */
export function keyAction(e: KeyLike): TicketKeyAction | null {
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  switch (e.key) {
    case 'j':
    case 'ArrowDown':
      return { type: 'move', delta: 1 };
    case 'k':
    case 'ArrowUp':
      return { type: 'move', delta: -1 };
    case 'Enter':
    case 'o':
      return { type: 'open' };
    case 'Escape':
      return { type: 'escape' };
    case 'x':
      return { type: 'toggleSelect' };
    case 's':
      return { type: 'status' };
    case 'a':
      return { type: 'assign' };
    case 'c':
      return { type: 'new' };
    case '/':
      return { type: 'search' };
    case 'b':
      return { type: 'toggleLayout' };
    case '[':
      return { type: 'paneTab', delta: -1 };
    case ']':
      return { type: 'paneTab', delta: 1 };
    case '?':
      return { type: 'help' };
    default:
      if (/^[1-6]$/.test(e.key)) return { type: 'view', index: Number(e.key) - 1 };
      return null;
  }
}

/** Keys that still work from inside the ticket slide-over (a modal dialog). */
const PANE_ACTIONS = new Set<TicketKeyAction['type']>(['move', 'status', 'assign', 'paneTab', 'help']);

const NON_TEXT_INPUTS = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file']);

/** True when the element takes typed text (the shortcuts stay off there). */
export function isTypingTarget(el: Element | null): boolean {
  if (!el || !(el instanceof HTMLElement)) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') return !NON_TEXT_INPUTS.has((el as HTMLInputElement).type);
  const role = el.getAttribute('role');
  return role === 'textbox' || role === 'combobox' || role === 'searchbox';
}

const OVERLAY_SELECTOR = '[role="menu"], [role="listbox"], [role="dialog"], [aria-modal="true"]';

/** An overlay counts only while it is on screen: the layout keeps a closed
 *  mobile nav drawer mounted (hidden, or transparent with pointer-events
 *  off), and that must not switch the shortcuts off. */
export function isShown(el: Element): boolean {
  if (el.closest('[aria-hidden="true"], [inert]')) return false;
  const check = (el as Element & { checkVisibility?: () => boolean }).checkVisibility;
  if (check && !check.call(el)) return false;
  const view = el.ownerDocument.defaultView;
  return !view || view.getComputedStyle(el).pointerEvents !== 'none';
}

/**
 * Where a key press may act: 'tab' (the list / board), 'pane' (inside the
 * ticket slide-over) or null (somewhere the shortcuts must stay off).
 */
export function keyScope(target: Element | null, root: Element | null, doc: Document = document): 'tab' | 'pane' | null {
  if (isTypingTarget(target)) return null;
  const overlays = Array.from(doc.querySelectorAll(OVERLAY_SELECTOR)).filter(isShown);
  // The ticket slide-over is the only overlay the keys work inside.
  const paneDialog = overlays.find((o) => o.getAttribute('role') === 'dialog' && !!o.querySelector('[data-ticket-pane]'));
  if (overlays.some((o) => o !== paneDialog)) return null;
  if (paneDialog) return target && paneDialog.contains(target) ? 'pane' : null;
  if (!target || target === doc.body || target === doc.documentElement) return 'tab';
  return root && root.contains(target) ? 'tab' : null;
}

export interface TicketKeyContext {
  scope: 'tab' | 'pane';
  event: KeyboardEvent;
}

/**
 * Listen for the shortcuts while `enabled`. `onAction` returns true when it
 * handled the key (the default action is then prevented). Registered in the
 * capture phase so a handled Escape never also reaches the pane's own
 * Escape (which closes it).
 */
export function useTicketKeys(
  rootRef: RefObject<HTMLElement | null>,
  onAction: (action: TicketKeyAction, ctx: TicketKeyContext) => boolean,
  enabled = true,
) {
  const handler = useRef(onAction);
  useEffect(() => {
    handler.current = onAction;
  });
  useEffect(() => {
    if (!enabled) return;
    function onKey(e: KeyboardEvent) {
      if (e.defaultPrevented || e.isComposing) return;
      const action = keyAction(e);
      if (!action) return;
      const target = (e.target instanceof Element ? e.target : null) ?? document.activeElement;
      const scope = keyScope(target, rootRef.current);
      if (!scope) return;
      if (scope === 'pane' && !PANE_ACTIONS.has(action.type)) return;
      if (handler.current(action, { scope, event: e })) e.preventDefault();
    }
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [enabled, rootRef]);
}

/** Shortcut rows for the help dialog, in the prototype's order. */
export const SHORTCUTS: { keys: string[]; label: string; sep?: string }[] = [
  { keys: ['J', 'K'], label: 'Next / previous ticket', sep: '/' },
  { keys: ['↵', 'O'], label: 'Open ticket', sep: 'or' },
  { keys: ['Esc'], label: 'Close panel / clear selection' },
  { keys: ['X'], label: 'Select ticket' },
  { keys: ['Shift+click'], label: 'Select a range' },
  { keys: ['S'], label: 'Change status' },
  { keys: ['A'], label: 'Assign' },
  { keys: ['C'], label: 'New ticket' },
  { keys: ['/'], label: 'Search' },
  { keys: ['B'], label: 'List / board' },
  { keys: ['1', '6'], label: 'Switch view', sep: '–' },
  { keys: ['[', ']'], label: 'Previous / next detail tab', sep: '/' },
  { keys: ['?'], label: 'This help' },
];
