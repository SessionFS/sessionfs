import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useHandoffInbox, useHandoffSent } from '../hooks/useHandoffs';
import RelativeDate from '../components/RelativeDate';
import type { HandoffSummary } from '../api/client';

type Tab = 'inbox' | 'sent';

function StatusBadge({ status }: { status: HandoffSummary['status'] }) {
  const styles = {
    pending: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/30',
    claimed: 'bg-green-500/10 text-green-400 border-green-500/30',
    expired: 'bg-neutral-500/10 text-neutral-400 border-neutral-500/30',
  };
  return (
    <span className={`px-1.5 py-0.5 text-sm border rounded ${styles[status]}`}>
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
    <div className="max-w-7xl mx-auto px-4 py-4">
      {/* Tabs */}
      <div className="flex items-center gap-1 mb-4 border-b border-border">
        <button
          onClick={() => setTab('inbox')}
          className={`px-3 py-2 text-sm -mb-px border-b-2 transition-colors ${
            isInbox
              ? 'border-accent text-accent'
              : 'border-transparent text-text-secondary hover:text-text-primary'
          }`}
        >
          Inbox
          {inbox.data && inbox.data.handoffs.filter((h) => h.status === 'pending').length > 0 && (
            <span className="ml-1.5 px-1.5 py-0.5 text-sm bg-yellow-500/20 text-yellow-400 rounded-full">
              {inbox.data.handoffs.filter((h) => h.status === 'pending').length}
            </span>
          )}
        </button>
        <button
          onClick={() => setTab('sent')}
          className={`px-3 py-2 text-sm -mb-px border-b-2 transition-colors ${
            !isInbox
              ? 'border-accent text-accent'
              : 'border-transparent text-text-secondary hover:text-text-primary'
          }`}
        >
          Sent
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
          Failed to load handoffs: {String(error)}
        </div>
      )}

      {/* Loading */}
      {isLoading && (
        <div className="text-center py-12 text-text-muted">Loading handoffs...</div>
      )}

      {/* Table */}
      {!isLoading && handoffs.length > 0 && (
        <div className="border border-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-bg-secondary text-text-secondary text-sm uppercase tracking-wider">
                <th className="px-3 py-2 text-left">{isInbox ? 'From' : 'To'}</th>
                <th className="px-3 py-2 text-left">Session</th>
                <th className="px-3 py-2 text-left w-24">Date</th>
                <th className="px-3 py-2 text-left w-20">Status</th>
              </tr>
            </thead>
            <tbody>
              {handoffs.map((h) => (
                <tr
                  key={h.id}
                  onClick={() => navigate(`/handoffs/${h.id}`)}
                  onKeyDown={(e) => { if (e.key === 'Enter') navigate(`/handoffs/${h.id}`); }}
                  tabIndex={0}
                  className="border-t border-border hover:bg-bg-tertiary cursor-pointer transition-colors focus:bg-bg-tertiary outline-none"
                >
                  <td className="px-3 py-2 text-text-secondary">
                    <div className="flex items-center gap-2">
                      <span className="w-6 h-6 rounded-full bg-bg-tertiary border border-border flex items-center justify-center text-sm text-text-muted uppercase">
                        {(isInbox ? h.sender_email : h.recipient_email).charAt(0)}
                      </span>
                      <span className="truncate max-w-[200px]">
                        {isInbox ? h.sender_email : h.recipient_email}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2 text-text-primary truncate max-w-xs">
                    {h.session_title || <span className="text-text-muted italic">Untitled</span>}
                  </td>
                  <td className="px-3 py-2 text-text-muted text-sm">
                    <RelativeDate iso={h.created_at} />
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={h.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Empty state */}
      {!isLoading && handoffs.length === 0 && !error && (
        <div className="text-center py-16">
          <p className="text-text-secondary mb-2">
            {isInbox ? 'No handoffs received' : 'No handoffs sent'}
          </p>
          <p className="text-text-muted text-sm">
            {isInbox
              ? 'When someone hands off a session to you, it will appear here.'
              : 'Hand off a session from its detail page to send it to a teammate.'}
          </p>
        </div>
      )}
    </div>
  );
}
