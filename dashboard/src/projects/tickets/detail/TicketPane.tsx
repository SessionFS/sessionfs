import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Ticket } from '../../../api/client';
import { Drawer } from '../../../components/ui';
import type { TicketJiraSurface } from '../constants';
import { isPaneTab, type PaneTab } from './nextStep';
import type { PaneMode } from './PaneHeader';
import { TicketPaneBody } from './TicketPaneBody';
import { isShown } from '../useTicketKeys';

/* ── Ticket pane ──
 *
 * One detail shape for every layout. In the list at 1360 px and wider it is
 * a split pane beside the list (a non-modal, labelled region; the list stays
 * usable). Narrower, and always from the board, it is a slide-over (a modal
 * dialog with a focus trap). Under 720 px the slide-over fills the screen
 * and leads with a back button. Escape closes it and focus returns to the
 * row it was opened from. */

export const SPLIT_QUERY = '(min-width: 1360px)';
export const PHONE_QUERY = '(max-width: 719px)';

function useMediaQuery(query: string): boolean {
  const get = () => typeof window !== 'undefined' && window.matchMedia?.(query).matches === true;
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const onChange = () => setMatches(mq.matches);
    onChange();
    mq.addEventListener?.('change', onChange);
    return () => mq.removeEventListener?.('change', onChange);
  }, [query]);
  return matches;
}

export interface TicketPaneProps {
  projectId: string;
  /** The open ticket, or null when the pane is closed. */
  ticketId: string | null;
  /** The tickets on screen, in order: prev/next walk this list. */
  tickets: Ticket[];
  viewMode: 'list' | 'board';
  /** Open another ticket from the visible list (prev/next). */
  onStep: (id: string) => void;
  /** Open any ticket (parent, child, blocker), even one the filters hide. */
  onNavigate: (id: string) => void;
  onClose: () => void;
  jira?: TicketJiraSurface;
  /** Tab from a deep link (`&ticket_tab=`), used for the first ticket opened. */
  initialTab?: string | null;
  /** Called when the reader picks another tab (the page mirrors it in the URL). */
  onTabChange?: (tab: PaneTab) => void;
  /** The list or board. In split mode it sits beside the pane. */
  children: ReactNode;
}

export function TicketPane({
  projectId,
  ticketId,
  tickets,
  viewMode,
  onStep,
  onNavigate,
  onClose,
  jira,
  initialTab,
  onTabChange,
  children,
}: TicketPaneProps) {
  const wide = useMediaQuery(SPLIT_QUERY);
  const phone = useMediaQuery(PHONE_QUERY);
  const mode: PaneMode = viewMode === 'list' && wide ? 'split' : phone ? 'phone' : 'drawer';
  const titleId = useId();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const paneRef = useRef<HTMLElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const [tab, setTab] = useState<PaneTab>(() => (isPaneTab(initialTab) ? initialTab : 'overview'));
  // The deep-linked tab applies to the first ticket only; moving to another
  // ticket starts on its overview.
  const [shownId, setShownId] = useState(ticketId);
  if (shownId !== ticketId) {
    setShownId(ticketId);
    if (shownId !== null || ticketId === null) setTab('overview');
  }

  const known = useMemo(() => new Map(tickets.map((t) => [t.id, t])), [tickets]);
  const index = ticketId ? tickets.findIndex((t) => t.id === ticketId) : -1;
  const prevId = index > 0 ? tickets[index - 1].id : null;
  const nextId = index >= 0 && index < tickets.length - 1 ? tickets[index + 1].id : null;

  // Where focus goes back to on close: the row for the ticket that is open
  // (rows may mark themselves with data-ticket-row), else whatever opened
  // the pane.
  const opener = useRef<HTMLElement | null>(null);
  const lastId = useRef<string | null>(null);
  useEffect(() => {
    if (ticketId && !lastId.current) {
      const active = document.activeElement as HTMLElement | null;
      opener.current = active && active !== document.body ? active : null;
    }
    lastId.current = ticketId;
  }, [ticketId]);

  const close = useCallback(() => {
    const id = lastId.current;
    onClose();
    if (mode !== 'split') return; // the drawer's focus trap restores focus itself
    requestAnimationFrame(() => {
      const row = id
        ? document.querySelector<HTMLElement>(`[data-ticket-row="${CSS.escape(id)}"]`)
        : null;
      const target = row ?? (opener.current?.isConnected ? opener.current : null);
      target?.focus();
    });
  }, [onClose, mode]);

  // Split mode is not a dialog, so Escape is handled here: it closes the
  // pane from inside it or from the list, unless something on top (a menu,
  // a dialog, an inline editor) claimed it first, or a comment is half typed.
  useEffect(() => {
    if (mode !== 'split' || !ticketId) return;
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      // (Only overlays on screen: the layout keeps a closed nav drawer mounted.)
      if (Array.from(document.querySelectorAll('[aria-modal="true"], [role="menu"], [role="listbox"]')).some(isShown)) return;
      const target = e.target as HTMLElement | null;
      const inPane = !!target && !!paneRef.current?.contains(target);
      const inList = !!target && !!listRef.current?.contains(target);
      if (!inPane && !inList && target !== document.body) return;
      if (target instanceof HTMLTextAreaElement && target.value.trim()) return;
      close();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [mode, ticketId, close]);

  // An opened ticket takes focus to its title, so a screen reader announces
  // it and Tab continues into the pane. (This runs after the slide-over's
  // focus trap has put focus on its first control.)
  useEffect(() => {
    if (ticketId) titleRef.current?.focus({ preventScroll: true });
  }, [mode, ticketId]);

  const fallback = ticketId ? known.get(ticketId) : undefined;
  const body = ticketId ? (
    <TicketPaneBody
      projectId={projectId}
      ticketId={ticketId}
      fallback={fallback}
      known={known}
      mode={mode}
      titleId={titleId}
      titleRef={titleRef}
      prevId={prevId}
      nextId={nextId}
      onStep={onStep}
      onNavigate={onNavigate}
      onClose={close}
      jira={jira}
      tab={tab}
      onTab={(next) => {
        setTab(next);
        onTabChange?.(next);
      }}
    />
  ) : null;

  if (mode === 'split') {
    return (
      <div
        className={ticketId ? 'grid grid-cols-[minmax(0,1fr)_minmax(400px,500px)] items-start gap-4' : undefined}
        data-ticket-pane-layout="split"
      >
        <div ref={listRef} className="tk-listcol">
          {children}
        </div>
        {ticketId && (
          <section
            ref={paneRef}
            aria-labelledby={titleId}
            data-ticket-pane="split"
            className="sticky top-4 max-h-[calc(100vh-2rem)] overflow-y-auto overscroll-contain rounded-lg border border-border bg-bg-elevated px-4 pb-4 pt-3"
          >
            {body}
          </section>
        )}
      </div>
    );
  }

  return (
    <>
      <div ref={listRef}>{children}</div>
      <Drawer
        open={!!ticketId}
        onClose={close}
        titleId={titleId}
        bare
        widthClassName={mode === 'phone' ? 'w-full' : 'w-[600px] max-w-full'}
      >
        <div className="flex-1 overflow-y-auto overscroll-contain px-4 pb-4 pt-3" data-ticket-pane={mode}>
          {body}
        </div>
      </Drawer>
    </>
  );
}
