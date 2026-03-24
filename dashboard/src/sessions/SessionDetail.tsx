import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useSession } from '../hooks/useSession';
import { abbreviateModel } from '../utils/models';
import { formatTokens } from '../utils/tokens';
import { estimateCost } from '../utils/cost';
import CopyButton from '../components/CopyButton';
import RelativeDate from '../components/RelativeDate';
import ConversationView from './ConversationView';
import AuditTab from './AuditTab';
import HandoffModal from '../handoffs/HandoffModal';

type Tab = 'messages' | 'audit';

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: session, isLoading, error } = useSession(id!);
  const [showHandoff, setShowHandoff] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>('messages');

  if (isLoading) {
    return <div className="p-8 text-text-muted">Loading session...</div>;
  }

  if (error || !session) {
    return (
      <div className="p-8">
        <button onClick={() => navigate('/')} className="text-accent text-sm mb-4 hover:underline">
          &larr; Back
        </button>
        <p className="text-red-400">Failed to load session: {String(error)}</p>
      </div>
    );
  }

  const totalTokens = session.total_input_tokens + session.total_output_tokens;
  const cost = estimateCost(session.model_id, session.total_input_tokens, session.total_output_tokens);
  const durationStr = session.duration_ms
    ? session.duration_ms >= 3600000
      ? `${(session.duration_ms / 3600000).toFixed(1)}h`
      : session.duration_ms >= 60000
        ? `${(session.duration_ms / 60000).toFixed(1)}m`
        : `${(session.duration_ms / 1000).toFixed(0)}s`
    : null;

  return (
    <div className="flex flex-col lg:flex-row min-h-0 flex-1">
      {/* Sidebar */}
      <aside className="lg:w-72 shrink-0 border-b lg:border-b-0 lg:border-r border-border bg-bg-secondary overflow-y-auto">
        <div className="p-4">
          <button
            onClick={() => navigate('/')}
            className="text-accent text-sm mb-3 hover:underline"
          >
            &larr; Back to Sessions
          </button>

          <h1 className="text-base font-medium text-text-primary mb-4 break-words">
            {session.title || 'Untitled session'}
          </h1>

          <Section title="Session">
            <Row label="ID" value={session.id} mono />
            <Row label="Tool" value={`${session.source_tool} ${session.source_tool_version || ''}`} />
            <Row label="Model" value={abbreviateModel(session.model_id)} />
            {session.original_session_id && (
              <Row label="Native ID" value={session.original_session_id} mono />
            )}
          </Section>

          <Section title="Stats">
            <Row label="Messages" value={String(session.message_count)} />
            <Row label="Turns" value={String(session.turn_count)} />
            <Row label="Tool uses" value={String(session.tool_use_count)} />
            <Row label="Input" value={formatTokens(session.total_input_tokens)} />
            <Row label="Output" value={formatTokens(session.total_output_tokens)} />
            <Row label="Total" value={formatTokens(totalTokens)} />
            {durationStr && <Row label="Duration" value={durationStr} />}
            {cost > 0 && <Row label="Est. cost" value={`$${cost.toFixed(4)}`} />}
          </Section>

          <Section title="Timestamps">
            <Row label="Created">
              <RelativeDate iso={session.created_at} />
            </Row>
            <Row label="Updated">
              <RelativeDate iso={session.updated_at} />
            </Row>
            <Row label="Uploaded">
              <RelativeDate iso={session.uploaded_at} />
            </Row>
          </Section>

          {session.tags.length > 0 && (
            <Section title="Tags">
              <div className="flex flex-wrap gap-1">
                {session.tags.map((t) => (
                  <span key={t} className="px-1.5 py-0.5 text-xs bg-bg-tertiary border border-border rounded text-text-secondary">
                    {t}
                  </span>
                ))}
              </div>
            </Section>
          )}

          <Section title="Actions">
            <div className="flex flex-col gap-2">
              <button
                onClick={() => setShowHandoff(true)}
                className="w-full px-3 py-1.5 text-xs bg-accent text-white rounded hover:bg-accent/90 transition-colors"
              >
                Hand Off
              </button>
              <div className="flex items-center gap-2">
                <code className="text-xs text-text-muted bg-bg-primary px-2 py-1 rounded flex-1 truncate">
                  sfs resume {session.id}
                </code>
                <CopyButton text={`sfs resume ${session.id}`} label="Copy" />
              </div>
              <div className="flex items-center gap-2">
                <code className="text-xs text-text-muted bg-bg-primary px-2 py-1 rounded flex-1 truncate">
                  sfs show {session.id}
                </code>
                <CopyButton text={`sfs show ${session.id}`} label="Copy" />
              </div>
            </div>
          </Section>
        </div>
      </aside>

      {/* Main content area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Tabs */}
        <div className="flex border-b border-border bg-bg-secondary shrink-0">
          <button
            onClick={() => setActiveTab('messages')}
            className={`px-4 py-2 text-xs font-medium transition-colors ${
              activeTab === 'messages'
                ? 'text-accent border-b-2 border-accent'
                : 'text-text-muted hover:text-text-secondary'
            }`}
          >
            Messages
          </button>
          <button
            onClick={() => setActiveTab('audit')}
            className={`px-4 py-2 text-xs font-medium transition-colors ${
              activeTab === 'audit'
                ? 'text-accent border-b-2 border-accent'
                : 'text-text-muted hover:text-text-secondary'
            }`}
          >
            Audit
          </button>
        </div>

        {/* Tab content */}
        <div className="flex-1 overflow-y-auto">
          {activeTab === 'messages' && <ConversationView sessionId={session.id} />}
          {activeTab === 'audit' && (
            <AuditTab sessionId={session.id} messageCount={session.message_count} />
          )}
        </div>
      </div>

      {showHandoff && (
        <HandoffModal sessionId={session.id} onClose={() => setShowHandoff(false)} />
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <h3 className="text-[10px] uppercase tracking-wider text-text-muted mb-1.5">{title}</h3>
      {children}
    </div>
  );
}

function Row({
  label,
  value,
  mono,
  children,
}: {
  label: string;
  value?: string;
  mono?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex justify-between text-xs py-0.5">
      <span className="text-text-muted">{label}</span>
      {children || (
        <span className={`text-text-secondary ${mono ? 'font-mono' : ''} truncate ml-2 max-w-[140px]`}>
          {value}
        </span>
      )}
    </div>
  );
}
