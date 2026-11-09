import { useCallback, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { BUBBLE_CLASSES, BUBBLE_WIDTH, useFloating, type FloatingSide } from '../components/ui/floating';
import { Tooltip } from '../components/ui/Tooltip';
import { docsUrl, getGlossaryEntry, type GlossaryId } from './glossary';

const HOVER_DELAY = 300;
const LEAVE_GRACE = 150;
const INTERACTIVE_ANCESTOR =
  'button, a[href], label, summary, select, textarea, [role="button"], [role="tab"], [role="link"], [role="menuitem"], [role="option"], [role="switch"], [role="checkbox"]';

interface TermProps {
  /** Glossary id (see help/glossary.ts). */
  id: GlossaryId;
  /** Visible text; defaults to the glossary term. */
  children?: ReactNode;
  /** Preferred side for the definition (it flips when there is no room). */
  side?: FloatingSide;
}

/**
 * A product term with its glossary definition.
 *
 * The label gets a dotted underline and is a real button, so it can be
 * reached by keyboard and tapped on a phone:
 * - mouse: hover shows the definition, leaving hides it (with a short grace
 *   period so the pointer can reach the "Learn more" link);
 * - keyboard: focus shows it, Tab moves into the link, Escape closes it;
 * - touch or click: a tap pins it open, a second tap or a tap elsewhere closes it.
 * The definition is always the trigger's description (aria-describedby), so
 * screen readers hear it whether or not the bubble is open.
 *
 * Never put a Term inside another button, link, label or tab: that nests
 * interactive elements. Put it next to the control instead.
 */
export function Term({ id, children, side = 'bottom' }: TermProps) {
  const entry = getGlossaryEntry(id);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const wrapRef = useRef<HTMLSpanElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const bubbleRef = useRef<HTMLSpanElement | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Set while Escape hands focus back to the trigger, so that focus doesn't
  // reopen what the user just closed.
  const returningFocus = useRef(false);
  const baseId = useId();
  const descId = `${baseId}-desc`;
  const bubbleId = `${baseId}-bubble`;
  const style = useFloating(open, triggerRef, bubbleRef, side, 'center');

  const close = useCallback(() => {
    clearTimeout(timer.current);
    setOpen(false);
    setPinned(false);
  }, []);

  useEffect(() => () => clearTimeout(timer.current), []);

  useEffect(() => {
    if (!import.meta.env.DEV || !wrapRef.current) return;
    const parent = wrapRef.current.parentElement?.closest(INTERACTIVE_ANCESTOR);
    if (parent) {
      console.error(`<Term id="${id}"> is inside an interactive element (${parent.tagName.toLowerCase()}). Move it next to the control.`);
    }
    if (!entry) console.error(`<Term id="${id}">: no glossary entry with that id.`);
  }, [id, entry]);

  // Escape closes it from anywhere, before a Dialog underneath sees the key.
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      const focusInside = wrapRef.current?.contains(document.activeElement);
      close();
      if (focusInside) {
        returningFocus.current = true;
        triggerRef.current?.focus();
        returningFocus.current = false;
      }
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, close]);

  // A tap or click outside closes it.
  useEffect(() => {
    if (!open) return;
    function onDown(e: Event) {
      if (wrapRef.current && wrapRef.current.contains(e.target as Node)) return;
      close();
    }
    document.addEventListener('pointerdown', onDown, true);
    return () => document.removeEventListener('pointerdown', onDown, true);
  }, [open, close]);

  if (!entry) return <>{children ?? id}</>;

  const isMouse = (t: string) => t !== 'touch' && t !== 'pen';

  return (
    <span
      ref={wrapRef}
      className="inline"
      data-term={id}
      onPointerEnter={(e) => {
        if (!isMouse(e.pointerType)) return;
        clearTimeout(timer.current);
        if (!open) timer.current = setTimeout(() => setOpen(true), HOVER_DELAY);
      }}
      onPointerLeave={(e) => {
        if (!isMouse(e.pointerType) || pinned) return;
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setOpen(false), LEAVE_GRACE);
      }}
      onBlur={(e) => {
        if (wrapRef.current && wrapRef.current.contains(e.relatedTarget as Node | null)) return;
        close();
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-describedby={descId}
        aria-expanded={open}
        aria-controls={open ? bubbleId : undefined}
        className="inline cursor-help appearance-none border-0 bg-transparent p-0 m-0 align-baseline text-inherit [font:inherit] [letter-spacing:inherit] [text-transform:inherit] underline decoration-dotted decoration-text-tertiary decoration-1 underline-offset-[3px] rounded-sm outline-none hover:decoration-text-secondary focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        onFocus={(e) => {
          if (returningFocus.current) return;
          let keyboard = true;
          try {
            keyboard = e.currentTarget.matches(':focus-visible');
          } catch {
            keyboard = true;
          }
          if (keyboard) {
            clearTimeout(timer.current);
            setOpen(true);
          }
        }}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          clearTimeout(timer.current);
          if (pinned) {
            close();
          } else {
            setOpen(true);
            setPinned(true);
          }
        }}
      >
        {children ?? entry.term}
      </button>
      <span id={descId} hidden>
        {entry.definition}
      </span>
      {open && (
        <span
          ref={bubbleRef}
          id={bubbleId}
          style={{ ...style, boxShadow: 'var(--shadow-md)' }}
          className={`${BUBBLE_CLASSES} ${BUBBLE_WIDTH} block w-max px-3 py-2`}
        >
          <span className="block font-semibold text-text-primary">{entry.term}</span>
          <span className="mt-0.5 block text-text-secondary" aria-hidden="true">
            {entry.definition}
          </span>
          {entry.docs && (
            <a
              href={docsUrl(entry.docs)}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1.5 inline-block font-medium text-brand underline-offset-2 hover:underline outline-none rounded-sm focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
              onClick={(e) => e.stopPropagation()}
            >
              Learn more{' '}
              <span className="sr-only">about {entry.term} (opens in a new tab)</span>
            </a>
          )}
        </span>
      )}
    </span>
  );
}

/**
 * Shows a glossary definition as the tooltip of an existing control: a button
 * that performs the action the term names, such as "Compile now". While
 * shown, the definition is also the control's description.
 */
export function TermTip({
  id,
  children,
  side = 'top',
}: {
  id: GlossaryId;
  children: ReactElement;
  side?: FloatingSide;
}) {
  const entry = getGlossaryEntry(id);
  if (!entry) return children;
  return (
    <Tooltip content={entry.definition} multiline side={side}>
      {children}
    </Tooltip>
  );
}
