import { useState, useEffect, useRef } from 'react';
import { useMessages } from '../hooks/useMessages';
import MessageBlock from './MessageBlock';
import { messageAnchorId } from './messageAnchor';
import { Button } from '../components/ui/Button';
import { Alert } from '../components/ui/Alert';
import { errorMessage } from '../utils/errorMessage';
import type { MessageTarget } from '../api/client';

const PAGE_SIZE = 50;
const HIGHLIGHT_MS = 2400;

interface Props {
  sessionId: string;
  /**
   * 0-based messages.jsonl line number to open at (the helper's `msg:<n>`,
   * from `?msg=<n>`). The page holding it is loaded, scrolled to and
   * briefly highlighted.
   */
  targetMessage?: number;
  /** Changes on every navigation, so following the same link again re-jumps. */
  targetKey?: string;
}

function targetNotice(target: MessageTarget): string | null {
  if (target.status === 'found') return null;
  if (target.anchor_index == null) {
    return `Message ${target.index} isn't shown in this view.`;
  }
  if (target.status === 'hidden') {
    return `Message ${target.index} is hidden in this view (it's a sidechain or empty entry). Showing the nearest message instead.`;
  }
  return `Message ${target.index} isn't in this transcript. Showing the closest message instead.`;
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

export default function ConversationView({ sessionId, targetMessage, targetKey }: Props) {
  const [page, setPage] = useState(1);
  // A cited message reads best in transcript order, like the audit jump.
  const [order, setOrder] = useState<'oldest' | 'newest'>(targetMessage != null ? 'oldest' : 'newest');
  // While set, the server picks the page that holds this message.
  const [locate, setLocate] = useState<number | null>(targetMessage ?? null);
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const handledRef = useRef<string | null>(null);

  // Reset when session changes (adjusted during render, before the target
  // check below, so a link into another session wins over the reset).
  const [prevSessionId, setPrevSessionId] = useState(sessionId);
  if (sessionId !== prevSessionId) {
    setPrevSessionId(sessionId);
    setPage(1);
    setOrder('newest');
    setLocate(null);
  }

  // A new deep link (or the same link followed again) re-locates.
  const targetToken = targetMessage != null ? `${sessionId}:${targetMessage}:${targetKey ?? ''}` : null;
  const [prevTargetToken, setPrevTargetToken] = useState(targetToken);
  if (targetToken !== prevTargetToken) {
    setPrevTargetToken(targetToken);
    if (targetMessage != null) {
      setLocate(targetMessage);
      setOrder('oldest');
    }
  }

  const { data, isLoading, error } = useMessages(sessionId, page, PAGE_SIZE, order, locate ?? undefined);
  const topRef = useRef<HTMLDivElement>(null);

  // The page on screen: when locating, the server chose it.
  const displayPage = data?.page ?? page;
  const target = locate != null && data?.target?.index === locate ? data.target : null;

  // Scroll to top when page changes (not while landing on a cited message)
  useEffect(() => {
    if (locate != null) return;
    topRef.current?.scrollIntoView({ behavior: 'smooth' });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only page changes scroll
  }, [displayPage]);

  // Land on the cited message once its page is rendered (once per link).
  const anchorIndex = target?.anchor_index ?? null;
  const notice = target ? targetNotice(target) : null;
  const landToken = target && anchorIndex != null ? `${sessionId}:${target.index}:${targetKey ?? ''}` : null;
  useEffect(() => {
    if (landToken == null || anchorIndex == null || handledRef.current === landToken) return;
    const frame = window.requestAnimationFrame(() => {
      handledRef.current = landToken;
      const el = document.getElementById(messageAnchorId(anchorIndex));
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
        el.focus({ preventScroll: true });
      }
      setHighlighted(anchorIndex);
      setAnnouncement(notice ?? `Showing message ${anchorIndex}.`);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [landToken, anchorIndex, notice]);

  // The highlight fades on its own.
  useEffect(() => {
    if (highlighted == null) return;
    const timer = window.setTimeout(() => setHighlighted(null), HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [highlighted]);

  function goToPage(p: number) {
    setLocate(null);
    setHighlighted(null);
    setPage(p);
  }

  if (error) {
    return (
      <div className="p-4 text-danger text-sm">
        Failed to load messages: {errorMessage(error)}
      </div>
    );
  }

  if (isLoading && !data) {
    return <div className="p-4 text-text-muted">Loading messages…</div>;
  }

  const messages = data?.messages || [];
  const indexes = data?.message_indexes;
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="flex flex-col gap-6 p-4">
      <div ref={topRef} />
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

      {/* Order toggle + pagination */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => { setOrder(o => o === 'newest' ? 'oldest' : 'newest'); goToPage(1); }}
          >
            {order === 'newest' ? 'Newest first' : 'Oldest first'}
          </Button>
          {order === 'oldest' && totalPages > 1 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => goToPage(totalPages)}
            >
              Jump to latest →
            </Button>
          )}
        </div>
        <span className="text-sm text-text-tertiary">
          {total} messages
        </span>
      </div>
      {notice && <Alert variant="info">{notice}</Alert>}
      {totalPages > 1 && (
        <Pagination page={displayPage} totalPages={totalPages} total={total} onPageChange={goToPage} isLoading={isLoading} />
      )}

      {messages.length === 0 && !isLoading && (
        <div className="flex flex-col items-center justify-center py-16">
          <svg className="w-10 h-10 text-text-tertiary mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
          <p className="text-md font-semibold text-text-primary mb-1">No messages yet</p>
          <p className="text-sm text-text-tertiary">This session contains no messages.</p>
        </div>
      )}

      {isLoading && messages.length === 0 && (
        <p className="text-text-muted text-sm">Loading page {displayPage}…</p>
      )}

      {messages.map((msg, i) => {
        const index = indexes?.[i];
        return (
          <MessageBlock
            key={index != null ? `m${index}` : `${displayPage}-${i}`}
            message={msg}
            index={index}
            highlighted={index != null && index === highlighted}
          />
        );
      })}

      {/* Bottom pagination */}
      {totalPages > 1 && (
        <Pagination page={displayPage} totalPages={totalPages} total={total} onPageChange={goToPage} isLoading={isLoading} />
      )}
    </div>
  );
}

function Pagination({
  page,
  totalPages,
  total,
  onPageChange,
  isLoading,
}: {
  page: number;
  totalPages: number;
  total: number;
  onPageChange: (p: number) => void;
  isLoading: boolean;
}) {
  // Build page numbers: show first, last, current, and neighbors
  const pages: (number | '…')[] = [];
  const addPage = (p: number) => { if (!pages.includes(p)) pages.push(p); };

  addPage(1);
  if (page > 3) pages.push('…');
  for (let p = Math.max(2, page - 1); p <= Math.min(totalPages - 1, page + 1); p++) {
    addPage(p);
  }
  if (page < totalPages - 2) pages.push('…');
  if (totalPages > 1) addPage(totalPages);

  return (
    <div className="flex items-center justify-between py-2">
      <span className="text-sm text-text-muted">
        {total} messages — Page {page} of {totalPages}
      </span>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1 || isLoading}
          className="px-2 py-1 text-sm border border-border rounded hover:border-text-muted transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-text-secondary"
        >
          Prev
        </button>
        {pages.map((p, i) =>
          p === '…' ? (
            <span key={`dots-${i}`} className="px-1 text-text-muted text-sm">…</span>
          ) : (
            <button
              key={p}
              onClick={() => onPageChange(p)}
              disabled={isLoading}
              className={`px-2.5 py-1 text-sm rounded transition-colors ${
                p === page
                  ? 'bg-accent text-white'
                  : 'border border-border text-text-secondary hover:border-text-muted'
              }`}
            >
              {p}
            </button>
          )
        )}
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages || isLoading}
          className="px-2 py-1 text-sm border border-border rounded hover:border-text-muted transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-text-secondary"
        >
          Next
        </button>
      </div>
    </div>
  );
}
