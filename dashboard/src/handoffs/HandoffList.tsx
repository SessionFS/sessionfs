import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useHandoffInbox, useHandoffSent } from '../hooks/useHandoffs';
import RelativeDate from '../components/RelativeDate';
import { Badge, ToolBadge } from '../components/Badge';
import { Tabs, TabPanel, PageContainer, PageHeader } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';
import { Term } from '../help/Term';
import { Icon } from '../components/ui/Icon';
import { Inbox, Send } from 'lucide-react';

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
        <span className="inline-flex items-center gap-1.5">
          Inbox
          {pendingCount > 0 && (
            <Badge variant="warning" label={String(pendingCount)} size="sm" />
          )}
        </span>
      ),
    },
    { key: 'sent', label: 'Sent' },
  ];

  return (
    <PageContainer>
      <PageHeader title={<Term id="handoff">Handoffs</Term>} />

      <Tabs
        tabs={tabItems}
        activeKey={tab}
        bare
        idPrefix="handoffs"
        aria-label="Handoff folders"
        onChange={(key) => setTab(key as Tab)}
      />

      <TabPanel idPrefix="handoffs" tabKey={tab}>
      {/* Error */}
      {error && (
        <div className="mt-4 mb-4 p-3 rounded-lg text-sm text-danger" style={{ backgroundColor: 'color-mix(in srgb, var(--danger) 10%, transparent)', border: '1px solid color-mix(in srgb, var(--danger) 30%, transparent)' }}>
          Failed to load handoffs: {errorMessage(error)}
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
                  <span className="w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-medium text-brand uppercase border" style={{ backgroundColor: 'color-mix(in srgb, var(--brand) 12%, transparent)', borderColor: 'color-mix(in srgb, var(--brand) 30%, transparent)' }}>
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
              <div className="mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-full bg-surface-active text-text-tertiary">
                <Icon icon={Inbox} size={20} />
              </div>
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
              <div className="mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-full bg-surface-active text-text-tertiary">
                <Icon icon={Send} size={20} />
              </div>
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
      </TabPanel>
    </PageContainer>
  );
}
