import { useState, useEffect, useRef } from 'react';
import { useMessages } from '../hooks/useMessages';
import MessageBlock from './MessageBlock';

const PAGE_SIZE = 50;

interface Props {
  sessionId: string;
  initialPage?: number;
}

export default function ConversationView({ sessionId, initialPage }: Props) {
  const [page, setPage] = useState(initialPage || 1);
  const [order, setOrder] = useState<'oldest' | 'newest'>('newest');

  // Jump to page when initialPage changes (from audit "Jump to message")
  useEffect(() => {
    if (initialPage) {
      setPage(initialPage);
      setOrder('oldest'); // Jump to message uses oldest-first indexing
    }
  }, [initialPage]);
  const { data, isLoading, error } = useMessages(sessionId, page, PAGE_SIZE, order);
  const prevSessionId = useRef(sessionId);
  const topRef = useRef<HTMLDivElement>(null);

  // Reset when session changes
  useEffect(() => {
    if (sessionId !== prevSessionId.current) {
      setPage(1);
      setOrder('newest');
      prevSessionId.current = sessionId;
    }
  }, [sessionId]);

  // Scroll to top when page changes
  useEffect(() => {
    topRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [page]);

  if (error) {
    return (
      <div className="p-4 text-red-400 text-sm">
        Failed to load messages: {String(error)}
      </div>
    );
  }

  if (isLoading && !data) {
    return <div className="p-4 text-text-muted">Loading messages...</div>;
  }

  const messages = data?.messages || [];
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="flex flex-col gap-3 p-4">
      <div ref={topRef} />

      {/* Order toggle + pagination */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <button
            onClick={() => { setOrder(o => o === 'newest' ? 'oldest' : 'newest'); setPage(1); }}
            className="text-[13px] text-[var(--text-tertiary)] hover:text-[var(--text-primary)] px-3 py-1.5 border border-[var(--border)] rounded-lg transition-colors"
          >
            {order === 'newest' ? 'Newest first' : 'Oldest first'}
          </button>
          {order === 'oldest' && totalPages > 1 && (
            <button
              onClick={() => setPage(totalPages)}
              className="text-[13px] text-[var(--brand)] hover:underline"
            >
              Jump to latest →
            </button>
          )}
        </div>
        <span className="text-[13px] text-[var(--text-tertiary)]">
          {total} messages
        </span>
      </div>
      {totalPages > 1 && (
        <Pagination page={page} totalPages={totalPages} total={total} onPageChange={setPage} isLoading={isLoading} />
      )}

      {messages.length === 0 && !isLoading && (
        <p className="text-text-muted text-sm">No messages in this session.</p>
      )}

      {isLoading && messages.length === 0 && (
        <p className="text-text-muted text-sm">Loading page {page}...</p>
      )}

      {messages.map((msg, i) => (
        <MessageBlock key={`${page}-${i}`} message={msg} />
      ))}

      {/* Bottom pagination */}
      {totalPages > 1 && (
        <Pagination page={page} totalPages={totalPages} total={total} onPageChange={setPage} isLoading={isLoading} />
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
  const pages: (number | '...')[] = [];
  const addPage = (p: number) => { if (!pages.includes(p)) pages.push(p); };

  addPage(1);
  if (page > 3) pages.push('...');
  for (let p = Math.max(2, page - 1); p <= Math.min(totalPages - 1, page + 1); p++) {
    addPage(p);
  }
  if (page < totalPages - 2) pages.push('...');
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
          p === '...' ? (
            <span key={`dots-${i}`} className="px-1 text-text-muted text-sm">...</span>
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
