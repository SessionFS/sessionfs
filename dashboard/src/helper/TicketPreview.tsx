/**
 * Ticket preview opened from a `tk_…` reference in a helper answer. A
 * pop-over anchored to the reference on wider drawers, a bottom sheet on
 * phones; either way it stays inside the drawer so the conversation is kept.
 * Esc or a click outside closes it and focus goes back to the reference.
 */

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, type TicketPreview as TicketPreviewData } from '../api/client';
import { Button, Markdown } from '../components/ui';
import { useTicketPreview } from '../hooks/useTickets';
import { errorMessage } from '../utils/errorMessage';
import { KindBadge } from '../projects/tickets/KindBadge';
import { PRIORITY_TONE, STATUS_DOT, formatStatusLabel } from '../projects/tickets/constants';
import { relativeTime } from './format';
import { CloseIcon } from './icons';
import { linkButtonSmall } from './styles';

const NARROW_QUERY = '(max-width: 639px)';
const GAP = 6;
const INSET = 12;
const MAX_WIDTH = 400;

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Where the ticket lives in the project page (the Work → Tickets list, expanded). */
function ticketPageHref(projectId: string, ticketId: string): string {
  return `/projects/${encodeURIComponent(projectId)}?tab=work&sub=tickets&ticket=${encodeURIComponent(ticketId)}`;
}

/** Own-key lookup, so a stray value like `constructor` can't resolve. */
function own(map: Record<string, string>, key: string): string | undefined {
  return Object.hasOwn(map, key) ? map[key] : undefined;
}

function sentenceCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function isNarrow(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.(NARROW_QUERY).matches === true;
}

/** Pop-over geometry relative to the overlay (the drawer panel), in px. */
function placeNear(overlay: DOMRect, trigger: DOMRect) {
  const width = Math.min(MAX_WIDTH, overlay.width - INSET * 2);
  const left = Math.min(Math.max(trigger.left - overlay.left - 16, INSET), overlay.width - width - INSET);
  const below = overlay.bottom - trigger.bottom - GAP - INSET;
  const above = trigger.top - overlay.top - GAP - INSET;
  if (below >= 300 || below >= above) {
    return { left, width, top: trigger.bottom - overlay.top + GAP, bottom: null, maxHeight: Math.max(below, 160) };
  }
  return { left, width, top: null, bottom: overlay.bottom - trigger.top + GAP, maxHeight: Math.max(above, 160) };
}

const px = (n: number | null) => (n == null ? '' : `${Math.round(n)}px`);

export default function TicketPreview({
  ticketId,
  trigger,
  onClose,
  onNavigate,
}: {
  ticketId: string;
  trigger: HTMLElement;
  onClose: () => void;
  onNavigate: () => void;
}) {
  const titleId = useId();
  const overlayRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(isNarrow);
  const query = useTicketPreview(ticketId);

  useEffect(() => {
    const mq = window.matchMedia?.(NARROW_QUERY);
    if (!mq) return;
    const onChange = () => setNarrow(mq.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  // The pop-over is positioned straight on the element (measured each time
  // the content or the layout changes); the bottom sheet uses its classes.
  const place = useCallback(() => {
    const card = cardRef.current;
    const overlay = overlayRef.current;
    if (!card || !overlay) return;
    if (narrow) {
      Object.assign(card.style, { left: '', width: '', top: '', bottom: '', maxHeight: '', visibility: '' });
      return;
    }
    const pos = placeNear(overlay.getBoundingClientRect(), trigger.getBoundingClientRect());
    Object.assign(card.style, {
      left: px(pos.left),
      width: px(pos.width),
      top: px(pos.top),
      bottom: px(pos.bottom),
      maxHeight: px(pos.maxHeight),
      visibility: '',
    });
  }, [narrow, trigger]);

  useLayoutEffect(() => {
    place();
  }, [place, query.status]);

  // Follow the reference when the transcript scrolls or the window resizes.
  useEffect(() => {
    let frame = 0;
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(place);
    };
    window.addEventListener('resize', schedule);
    document.addEventListener('scroll', schedule, true);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', schedule);
      document.removeEventListener('scroll', schedule, true);
    };
  }, [place]);

  // Focus moves into the preview; it returns to the reference on close.
  useEffect(() => {
    cardRef.current?.focus();
    return () => {
      if (trigger.isConnected) trigger.focus();
    };
  }, [trigger]);

  // Esc closes only the preview: captured and consumed before the drawer's
  // own Escape handler sees it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      onClose();
    };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  // A press outside the preview (other than on its own reference, which
  // toggles) closes it.
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node | null;
      if (!target || cardRef.current?.contains(target) || trigger.contains(target)) return;
      onClose();
    };
    document.addEventListener('pointerdown', onDown, true);
    return () => document.removeEventListener('pointerdown', onDown, true);
  }, [onClose, trigger]);

  // Keep Tab inside the preview. Handled on the card itself and stopped there
  // so the drawer's own focus trap doesn't act on the same key press.
  useEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    const onTab = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      e.stopPropagation();
      const items = [...card.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === card)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    card.addEventListener('keydown', onTab);
    return () => card.removeEventListener('keydown', onTab);
  }, []);

  const shell =
    'pointer-events-auto flex flex-col overflow-hidden border border-border bg-overlay text-left outline-none';
  const cardClass = narrow
    ? `${shell} absolute inset-x-0 bottom-0 max-h-[85%] rounded-t-2xl border-b-0`
    : `${shell} absolute rounded-xl`;

  return (
    <div ref={overlayRef} className="pointer-events-none absolute inset-0 z-30">
      {narrow && <div className="pointer-events-auto absolute inset-0" style={{ backgroundColor: 'var(--scrim)' }} aria-hidden="true" />}
      <div
        ref={cardRef}
        role="dialog"
        aria-modal={narrow}
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cardClass}
        style={{
          boxShadow: 'var(--shadow-overlay)',
          animation: 'overlayEnter 120ms ease-out both',
        }}
      >
        {narrow && <div className="mx-auto mt-2 h-1 w-9 shrink-0 rounded-full bg-border-strong" aria-hidden="true" />}
        <div className="flex shrink-0 items-center gap-2 px-4 pb-1 pt-3">
          {query.data && <KindBadge kind={query.data.kind} />}
          <span className="min-w-0 truncate font-mono text-2xs text-text-tertiary">{ticketId}</span>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close ticket preview"
            className="-mr-1.5 ml-auto rounded-md p-1.5 text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
          >
            <CloseIcon size={15} />
          </button>
        </div>
        <div className="min-h-0 overflow-y-auto overscroll-contain px-4 pb-4">
          <PreviewBody
            titleId={titleId}
            ticketId={ticketId}
            query={query}
            onNavigate={onNavigate}
          />
        </div>
      </div>
    </div>
  );
}

function PreviewBody({
  titleId,
  ticketId,
  query,
  onNavigate,
}: {
  titleId: string;
  ticketId: string;
  query: ReturnType<typeof useTicketPreview>;
  onNavigate: () => void;
}) {
  if (query.isPending) {
    return (
      <div aria-busy="true" aria-live="polite" className="space-y-2.5 pt-1">
        <h2 id={titleId} className="sr-only">
          Loading ticket {ticketId}
        </h2>
        <div className="h-4 w-4/5 animate-skeleton-pulse rounded bg-surface-hover" />
        <div className="flex gap-2">
          <div className="h-4 w-16 animate-skeleton-pulse rounded-full bg-surface-hover" />
          <div className="h-4 w-12 animate-skeleton-pulse rounded-full bg-surface-hover" />
        </div>
        <div className="h-3 w-full animate-skeleton-pulse rounded bg-surface-hover" />
        <div className="h-3 w-11/12 animate-skeleton-pulse rounded bg-surface-hover" />
        <div className="h-3 w-2/3 animate-skeleton-pulse rounded bg-surface-hover" />
      </div>
    );
  }
  if (query.isError) {
    const missing = query.error instanceof ApiError && query.error.status === 404;
    return (
      <div className="pt-1">
        <h2 id={titleId} className="text-sm font-semibold text-text-primary">
          {missing ? 'Ticket not available' : 'Couldn’t load this ticket'}
        </h2>
        <p className="mt-1 text-sm text-text-secondary">
          {missing
            ? 'It doesn’t exist, or you don’t have access to its project.'
            : errorMessage(query.error, 'Something went wrong loading the ticket.')}
        </p>
        {!missing && (
          <Button size="sm" variant="secondary" className="mt-3" onClick={() => void query.refetch()}>
            Retry
          </Button>
        )}
      </div>
    );
  }
  return <PreviewDetails titleId={titleId} ticket={query.data} onNavigate={onNavigate} />;
}

function PreviewDetails({
  titleId,
  ticket,
  onNavigate,
}: {
  titleId: string;
  ticket: TicketPreviewData;
  onNavigate: () => void;
}) {
  const criteria = ticket.acceptance_criteria.length;
  const description = ticket.description.trim();
  const assignee = ticket.assigned_to;
  return (
    <div>
      <h2 id={titleId} className="text-md font-semibold leading-snug text-text-primary [overflow-wrap:anywhere]">
        {ticket.title}
      </h2>

      <dl className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-text-secondary">
        <div className="flex items-center gap-1">
          <dt className="sr-only">Status</dt>
          <dd className="inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5 font-medium text-text-primary">
            <span className={`h-1.5 w-1.5 rounded-full ${own(STATUS_DOT, ticket.status) ?? 'bg-text-tertiary'}`} aria-hidden="true" />
            {sentenceCase(formatStatusLabel(ticket.status))}
          </dd>
        </div>
        <div className="flex items-center gap-1">
          <dt className="text-text-tertiary">Priority</dt>
          <dd className={`font-medium capitalize ${own(PRIORITY_TONE, ticket.priority) ?? ''}`}>{ticket.priority}</dd>
        </div>
        <div className="flex min-w-0 items-center gap-1">
          <dt className="text-text-tertiary">Assignee</dt>
          <dd className="min-w-0 truncate font-medium text-text-primary">{assignee || 'Unassigned'}</dd>
        </div>
        <div className="flex min-w-0 basis-full items-center gap-1">
          <dt className="text-text-tertiary">Project</dt>
          <dd className="min-w-0 truncate text-text-primary">{ticket.project_name}</dd>
        </div>
      </dl>

      {description && (
        <div className="relative mt-3 max-h-[7.5rem] overflow-hidden [mask-image:linear-gradient(to_bottom,black_70%,transparent)] [&>div]:!text-sm [&>div]:!leading-6 [&_p]:!my-1 [&_ul]:!my-1 [&_ol]:!my-1 [&_h1]:!text-sm [&_h2]:!text-sm [&_h3]:!text-sm [&_h1]:!mt-1 [&_h2]:!mt-1 [&_h2]:!pt-0 [&_h2]:!border-t-0 [&_h3]:!mt-1">
          <Markdown>{description}</Markdown>
        </div>
      )}

      {criteria > 0 && (
        <p className="mt-2 text-xs text-text-tertiary">
          {criteria} acceptance {criteria === 1 ? 'criterion' : 'criteria'}
        </p>
      )}

      {ticket.recent_comments.length > 0 && (
        <section className="mt-3 border-t border-border pt-3" aria-label="Latest comments">
          <p className="mb-2 text-2xs font-semibold uppercase tracking-wide text-text-tertiary">
            Latest comments
            {ticket.comment_count > ticket.recent_comments.length && (
              <span className="font-normal normal-case tracking-normal"> · {ticket.comment_count} total</span>
            )}
          </p>
          <ul className="space-y-2.5">
            {ticket.recent_comments.map((c) => (
              <li key={c.id} className="min-w-0 text-xs">
                <div className="flex items-baseline gap-1.5">
                  <span className="font-medium text-text-primary">{c.author_persona || 'Member'}</span>
                  <time dateTime={c.created_at} className="text-text-tertiary">
                    {relativeTime(c.created_at)}
                  </time>
                </div>
                <p className="mt-0.5 line-clamp-3 whitespace-pre-line text-text-secondary [overflow-wrap:anywhere]">
                  {c.content}
                </p>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="mt-4 flex">
        <Link to={ticketPageHref(ticket.project_id, ticket.id)} onClick={onNavigate} className={linkButtonSmall}>
          Open ticket
        </Link>
      </div>
    </div>
  );
}
