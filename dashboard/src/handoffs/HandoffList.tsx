import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useHandoffInbox, useHandoffSent } from '../hooks/useHandoffs';
import RelativeDate from '../components/RelativeDate';
import { Badge, ToolBadge } from '../components/Badge';
import { Tabs } from '../components/ui';

type Tab = 'inbox' | 'sent';

/** Map handoff statuses to Badge variants. */
const STATUS_VARIANT: Record<string, 'warning' | 'success' | 'danger'> = {
  pending: 'warning',
  claimed: 'success',
  expired: 'danger',
};

export default function HandoffList() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>('inbox');

  const inbox = useHandoffInbox();
  const sent = useHandoffSent();

  const isInbox = tab === 'inbox';
  const { data, isLoading, error } = isInbox ? inbox : sent;
  const handoffs = data?.handoffs ?? [];

  const pendingCount = inbox.data
    ? inbox.data.handoffs.filter((h) => h.status === 'pending').length
    : 0;

  const tabItems = [
    {
      key: 'inbox',
      label: (
        <>
          Inbox
          {pendingCount > 0 && (
            <Badge variant="warning" label={String(pendingCount)} size="sm" />
          )}
        </>
      ),
    },
    { key: 'sent', label: 'Sent' },
  ];

  return (
    <div className="max-w-4xl mx-auto px-4 py-6">
      <h1 className="text-2xl font-bold tracking-tight text-text-primary mb-5">Handoffs</h1>

      <Tabs
        tabs={tabItems}
        activeKey={tab}
        bare
        onChange={(key) => setTab(key as Tab)}
      />

      {/* Error */}
      {error && (
        <div className="mt-4 mb-4 p-3 rounded-lg text-sm text-danger" style={{ backgroundColor: 'rgba(240,64,96,0.1)', border: '1px solid rgba(240,64,96,0.3)' }}>
          Failed to load handoffs: {String(error)}
        </div>
      )}

      {/* Loading */}
      {isLoading && (
        <div className="text-center py-12 text-text-tertiary text-sm">Loading handoffs…</div>
      )}

      {/* Handoff cards */}
      {!isLoading && handoffs.length > 0 && (
        <div className="mt-4 space-y-3">
          {handoffs.map((h) => (
            <div
              key={h.id}
              role="button"
              onClick={() => navigate(`/handoffs/${h.id}`)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate(`/handoffs/${h.id}`); } }}
              tabIndex={0}
              className="rounded-xl p-4 cursor-pointer border border-border hover:border-border-strong transition-colors duration-150 focus:border-[var(--brand)] outline-none bg-surface"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <span className="w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-medium text-brand uppercase border" style={{ backgroundColor: 'rgba(79,156,247,0.12)', borderColor: 'rgba(79,156,247,0.3)' }}>
                    {(isInbox ? h.sender_email : h.recipient_email).charAt(0)}
                  </span>
                  <div className="min-w-0">
                    <div className="text-base font-medium text-text-primary truncate">
                      {isInbox ? h.sender_email : h.recipient_email}
                    </div>
                    <div className="text-sm text-text-tertiary">
                      <RelativeDate iso={h.created_at} />
                    </div>
                  </div>
                </div>
                <Badge
                  variant={STATUS_VARIANT[h.status] ?? 'default'}
                  tint
                  label={h.status}
                  size="sm"
                />
              </div>

              {/* Session info */}
              <div className="mt-3 flex items-center gap-3 text-sm">
                {h.session_tool && <ToolBadge tool={h.session_tool} />}
                <span className="text-text-primary truncate">
                  {h.session_title || <span className="text-text-tertiary italic">Untitled</span>}
                </span>
                {h.session_message_count != null && (
                  <span className="text-text-tertiary tabular-nums flex-shrink-0">
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
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="mx-auto mb-4 opacity-30">
                <polyline points="22 12 16 12 14 15 10 15 8 12 2 12" />
                <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
              </svg>
              <p className="text-md font-semibold text-text-primary mb-1">No handoffs received yet</p>
              <p className="text-sm text-text-tertiary mb-4">
                When a teammate hands off a session to you, it will appear here.
              </p>
              <a
                href="https://sessionfs.dev/handoff/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-brand hover:underline"
              >
                Learn about handoffs →
              </a>
            </>
          ) : (
            <>
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="mx-auto mb-4 opacity-30">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
              <p className="text-md font-semibold text-text-primary mb-1">No handoffs sent yet</p>
              <p className="text-sm text-text-tertiary mb-4">
                Hand off a session to share context with a teammate.
              </p>
              <a
                href="https://sessionfs.dev/handoff/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-brand hover:underline"
              >
                Hand off a session →
              </a>
            </>
          )}
        </div>
      )}
    </div>
  );
}
