import { useState, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useSession } from '../hooks/useSession';
import { useAudit } from '../hooks/useAudit';
import { useFolders, useAddBookmark } from '../hooks/useBookmarks';
import { useAuth } from '../auth/AuthContext';
import { abbreviateModel } from '../utils/models';
import { formatTokens } from '../utils/tokens';
import { estimateCost } from '../utils/cost';
import CopyButton from '../components/CopyButton';
import RelativeDate from '../components/RelativeDate';
import ConversationView from './ConversationView';
import AuditTab from './AuditTab';
import AuditModal from './AuditModal';
import SummaryTab from './SummaryTab';
import HandoffModal from '../handoffs/HandoffModal';

type Tab = 'messages' | 'summary' | 'audit';

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: session, isLoading, error, refetch } = useSession(id!);
  const { data: auditReport } = useAudit(id!);
  const { auth } = useAuth();
  const [showHandoff, setShowHandoff] = useState(false);
  const [showAuditModal, setShowAuditModal] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>('messages');
  const [jumpToPage, setJumpToPage] = useState<number | undefined>(undefined);

  function handleJumpToMessage(messageIndex: number) {
    const page = Math.floor(messageIndex / 50) + 1;
    setJumpToPage(page);
    setActiveTab('messages');
  }
  const [editingAlias, setEditingAlias] = useState(false);
  const [aliasInput, setAliasInput] = useState('');
  const [aliasError, setAliasError] = useState<string | null>(null);

  const handleAliasEdit = useCallback(() => {
    setAliasInput(session?.alias || '');
    setAliasError(null);
    setEditingAlias(true);
  }, [session?.alias]);

  const handleAliasSave = useCallback(async () => {
    if (!auth || !session) return;
    const trimmed = aliasInput.trim();
    try {
      if (trimmed) {
        await auth.client.setAlias(session.id, trimmed);
      } else {
        await auth.client.clearAlias(session.id);
      }
      setEditingAlias(false);
      setAliasError(null);
      refetch();
    } catch (err) {
      setAliasError(String(err));
    }
  }, [auth, session, aliasInput, refetch]);

  const handleAliasKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter') handleAliasSave();
      if (e.key === 'Escape') setEditingAlias(false);
    },
    [handleAliasSave],
  );

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
            <Row label="Alias">
              {editingAlias ? (
                <div className="flex flex-col items-end gap-1">
                  <input
                    type="text"
                    value={aliasInput}
                    onChange={(e) => setAliasInput(e.target.value)}
                    onKeyDown={handleAliasKeyDown}
                    onBlur={handleAliasSave}
                    autoFocus
                    placeholder="e.g. auth-debug"
                    className="w-[140px] px-1.5 py-0.5 text-sm bg-bg-primary border border-border rounded text-text-primary focus:outline-none focus:border-accent"
                  />
                  {aliasError && (
                    <span className="text-red-400 text-[10px]">{aliasError}</span>
                  )}
                </div>
              ) : (
                <span
                  onClick={handleAliasEdit}
                  className="text-text-secondary text-sm cursor-pointer hover:text-accent truncate ml-2 max-w-[140px]"
                  title="Click to edit alias"
                >
                  {session.alias || <span className="text-text-muted italic">Set alias</span>}
                </span>
              )}
            </Row>
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
                  <span key={t} className="px-1.5 py-0.5 text-sm bg-bg-tertiary border border-border rounded text-text-secondary">
                    {t}
                  </span>
                ))}
              </div>
            </Section>
          )}

          <BookmarksSection sessionId={session.id} />

          <Section title="Audit Status">
            {auditReport ? (
              <div className="space-y-2">
                <AuditScoreBar score={auditReport.summary.trust_score} />
                <div className="text-sm text-text-muted">
                  Last audited: <RelativeDate iso={auditReport.timestamp} /> ({auditReport.model})
                </div>
                <button
                  onClick={() => setActiveTab('audit')}
                  className="text-sm text-accent hover:underline"
                >
                  View Report
                </button>
              </div>
            ) : (
              <div className="space-y-2">
                <span className="text-sm text-text-muted">Not audited</span>
                <button
                  onClick={() => setShowAuditModal(true)}
                  className="w-full px-3 py-1.5 text-sm border border-border text-text-secondary rounded hover:bg-bg-tertiary transition-colors"
                >
                  Run Audit
                </button>
              </div>
            )}
          </Section>

          <Section title="Actions">
            <div className="flex flex-col gap-2">
              <button
                onClick={() => setShowHandoff(true)}
                className="w-full px-3 py-1.5 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors"
              >
                Hand Off
              </button>
              <div className="flex items-center gap-2">
                <code className="text-sm text-text-muted bg-bg-primary px-2 py-1 rounded flex-1 truncate">
                  sfs resume {session.id}
                </code>
                <CopyButton text={`sfs resume ${session.id}`} label="Copy" />
              </div>
              <div className="flex items-center gap-2">
                <code className="text-sm text-text-muted bg-bg-primary px-2 py-1 rounded flex-1 truncate">
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
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === 'messages'
                ? 'text-accent border-b-2 border-accent'
                : 'text-text-muted hover:text-text-secondary'
            }`}
          >
            Messages
          </button>
          <button
            onClick={() => setActiveTab('summary')}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === 'summary'
                ? 'text-accent border-b-2 border-accent'
                : 'text-text-muted hover:text-text-secondary'
            }`}
          >
            Summary
          </button>
          <button
            onClick={() => setActiveTab('audit')}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
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
          {activeTab === 'messages' && <ConversationView sessionId={session.id} initialPage={jumpToPage} />}
          {activeTab === 'summary' && <SummaryTab sessionId={session.id} />}
          {activeTab === 'audit' && (
            <AuditTab sessionId={session.id} messageCount={session.message_count} sessionTitle={session.title || undefined} onJumpToMessage={handleJumpToMessage} />
          )}
        </div>
      </div>

      {showHandoff && (
        <HandoffModal sessionId={session.id} onClose={() => setShowHandoff(false)} />
      )}
      {showAuditModal && (
        <AuditModal
          sessionId={session.id}
          messageCount={session.message_count}
          onClose={() => setShowAuditModal(false)}
          onComplete={() => setShowAuditModal(false)}
        />
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
    <div className="flex justify-between text-sm py-0.5">
      <span className="text-text-muted">{label}</span>
      {children || (
        <span className={`text-text-secondary ${mono ? 'font-mono' : ''} truncate ml-2 max-w-[140px]`}>
          {value}
        </span>
      )}
    </div>
  );
}

function BookmarksSection({ sessionId }: { sessionId: string }) {
  const { data: foldersData } = useFolders();
  const addBookmark = useAddBookmark();
  const [showAdd, setShowAdd] = useState(false);

  const folders = foldersData?.folders ?? [];

  // We check all folders' sessions to see if this session is bookmarked.
  // For simplicity, we track locally after mutations; the real source of truth
  // is the folder sessions endpoint, but we avoid N+1 queries here.
  // The bookmark IDs are not available from the folder list, so we show
  // folder names and let users add/remove via the add dropdown.

  return (
    <Section title="Bookmarks">
      <div className="space-y-1">
        {folders.length === 0 && (
          <span className="text-sm text-text-muted">No folders created</span>
        )}
        {folders.length > 0 && !showAdd && (
          <button
            onClick={() => setShowAdd(true)}
            className="text-sm text-accent hover:underline"
          >
            + Add to folder
          </button>
        )}
        {showAdd && (
          <div className="space-y-1">
            {folders.map((f) => (
              <button
                key={f.id}
                onClick={() => {
                  addBookmark.mutate({ folderId: f.id, sessionId }, {
                    onSettled: () => setShowAdd(false),
                  });
                }}
                className="w-full text-left px-2 py-1 text-sm text-text-secondary hover:bg-bg-tertiary rounded flex items-center gap-2"
              >
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: f.color || '#4f9cf7' }}
                />
                {f.name}
              </button>
            ))}
            <button
              onClick={() => setShowAdd(false)}
              className="text-xs text-text-muted hover:text-text-secondary"
            >
              Cancel
            </button>
          </div>
        )}
      </div>
    </Section>
  );
}

function AuditScoreBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color =
    pct >= 90 ? 'bg-green-500' : pct >= 70 ? 'bg-yellow-500' : 'bg-red-500';
  const textColor =
    pct >= 90 ? 'text-green-400' : pct >= 70 ? 'text-yellow-400' : 'text-red-400';

  return (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-sm text-text-muted">Trust</span>
        <span className={`text-sm font-semibold tabular-nums ${textColor}`}>{pct}%</span>
      </div>
      <div className="h-1.5 bg-bg-tertiary rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
