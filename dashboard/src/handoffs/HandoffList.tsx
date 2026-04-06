import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useHandoffInbox, useHandoffSent } from '../hooks/useHandoffs';
import RelativeDate from '../components/RelativeDate';
import { ToolBadge } from '../components/Badge';
import type { HandoffSummary } from '../api/client';

type Tab = 'inbox' | 'sent';

function StatusBadge({ status }: { status: HandoffSummary['status'] }) {
  const styles = {
    pending: 'bg-yellow-500/10 text-yellow-500 border-yellow-500/30',
    claimed: 'bg-green-500/10 text-green-500 border-green-500/30',
    expired: 'bg-red-500/10 text-red-500 border-red-500/30',
  };
  return (
    <span className={`px-2 py-0.5 text-xs font-medium border rounded-full ${styles[status]}`}>
      {status}
    </span>
  );
}

export default function HandoffList() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>('inbox');

  const inbox = useHandoffInbox();
  const sent = useHandoffSent();

  const isInbox = tab === 'inbox';
  const { data, isLoading, error } = isInbox ? inbox : sent;
  const handoffs = data?.handoffs ?? [];

  return (
    <div className="max-w-4xl mx-auto px-4 py-6">
      <h1 className="text-3xl font-bold tracking-tight text-[var(--text-primary)] mb-5">Handoffs</h1>

      {/* Tabs */}
      <div className="flex items-center gap-0 mb-6 border-b border-[var(--border)]">
        <button
          onClick={() => setTab('inbox')}
          className={`px-4 py-2 text-[14px] font-medium transition-colors ${
            isInbox
              ? 'border-b-2 border-[var(--brand)] text-[var(--text-primary)] -mb-px'
              : 'text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
          }`}
        >
          Inbox
          {inbox.data && inbox.data.handoffs.filter((h) => h.status === 'pending').length > 0 && (
            <span className="ml-1.5 px-1.5 py-0.5 text-xs font-medium bg-yellow-500/15 text-yellow-500 rounded-full">
              {inbox.data.handoffs.filter((h) => h.status === 'pending').length}
            </span>
          )}
        </button>
        <button
          onClick={() => setTab('sent')}
          className={`px-4 py-2 text-[14px] font-medium transition-colors ${
            !isInbox
              ? 'border-b-2 border-[var(--brand)] text-[var(--text-primary)] -mb-px'
              : 'text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
          }`}
        >
          Sent
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
          Failed to load handoffs: {String(error)}
        </div>
      )}

      {/* Loading */}
      {isLoading && (
        <div className="text-center py-12 text-[var(--text-tertiary)]">Loading handoffs...</div>
      )}

      {/* Handoff cards */}
      {!isLoading && handoffs.length > 0 && (
        <div className="space-y-3">
          {handoffs.map((h) => (
            <div
              key={h.id}
              role="button"
              onClick={() => navigate(`/handoffs/${h.id}`)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate(`/handoffs/${h.id}`); } }}
              tabIndex={0}
              className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-4 cursor-pointer hover:border-[var(--border-strong)] transition-colors focus:border-[var(--brand)] outline-none"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  {/* Avatar */}
                  <span className="w-8 h-8 rounded-full bg-[var(--brand)]/15 border border-[var(--brand)]/30 flex-shrink-0 flex items-center justify-center text-xs font-medium text-[var(--brand)] uppercase">
                    {(isInbox ? h.sender_email : h.recipient_email).charAt(0)}
                  </span>
                  <div className="min-w-0">
                    <div className="text-[14px] font-medium text-[var(--text-primary)] truncate">
                      {isInbox ? h.sender_email : h.recipient_email}
                    </div>
                    <div className="text-[13px] text-[var(--text-tertiary)]">
                      <RelativeDate iso={h.created_at} />
                    </div>
                  </div>
                </div>
                <StatusBadge status={h.status} />
              </div>

              {/* Session info */}
              <div className="mt-3 flex items-center gap-3 text-[13px]">
                {h.session_tool && <ToolBadge tool={h.session_tool} />}
                <span className="text-[var(--text-primary)] truncate">
                  {h.session_title || <span className="text-[var(--text-tertiary)] italic">Untitled</span>}
                </span>
                {h.session_message_count != null && (
                  <span className="text-[var(--text-tertiary)] tabular-nums flex-shrink-0">
                    {h.session_message_count} msgs
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Empty state */}
      {!isLoading && handoffs.length === 0 && !error && (
        <div className="text-center py-16">
          {isInbox ? (
            <>
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="mx-auto mb-4 opacity-40">
                <polyline points="22 12 16 12 14 15 10 15 8 12 2 12" />
                <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
              </svg>
              <p className="text-xl font-semibold text-[var(--text-primary)] mb-1">No handoffs received yet</p>
              <p className="text-[var(--text-tertiary)] text-sm mb-4">
                When a teammate hands off a session to you, it will appear here.
              </p>
              <a
                href="https://docs.sessionfs.dev/handoffs"
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-[var(--brand)] hover:underline"
              >
                Learn about handoffs &rarr;
              </a>
            </>
          ) : (
            <>
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="mx-auto mb-4 opacity-40">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
              <p className="text-xl font-semibold text-[var(--text-primary)] mb-1">No handoffs sent yet</p>
              <p className="text-[var(--text-tertiary)] text-sm mb-4">
                Hand off a session to share context with a teammate.
              </p>
              <a
                href="https://docs.sessionfs.dev/handoffs"
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-[var(--brand)] hover:underline"
              >
                Hand off a session &rarr;
              </a>
            </>
          )}
        </div>
      )}
    </div>
  );
}
