import { useState, useCallback } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { useSession } from '../hooks/useSession';
import { useMessages } from '../hooks/useMessages';
import { useAudit } from '../hooks/useAudit';
import { useFolders, useAddBookmark } from '../hooks/useBookmarks';
import { useAuth } from '../auth/AuthContext';
import { abbreviateModel, fullToolName } from '../utils/models';
import { formatTokens } from '../utils/tokens';
import { estimateCost } from '../utils/cost';
import CopyButton from '../components/CopyButton';
import RelativeDate from '../components/RelativeDate';
import ConversationView from './ConversationView';
import AuditTab from './AuditTab';
import AuditModal from './AuditModal';
import SummaryTab from './SummaryTab';
import HandoffModal from '../handoffs/HandoffModal';

const TOOL_COLORS: Record<string, string> = {
  'claude-code': 'var(--tool-claude)',
  cursor: 'var(--tool-cursor)',
  codex: 'var(--tool-codex)',
  gemini: 'var(--tool-gemini)',
  copilot: 'var(--tool-copilot)',
  amp: 'var(--tool-amp)',
  cline: 'var(--tool-cline)',
  'roo-code': 'var(--tool-roo)',
};

type Tab = 'messages' | 'summary' | 'audit';

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: session, isLoading, error, refetch } = useSession(id!);
  const { data: auditReport } = useAudit(id!);

  // Fetch last page of messages for Quick Preview
  const lastMsgPage = session ? Math.max(1, Math.ceil(session.message_count / 50)) : 1;
  const { data: lastMessagesData } = useMessages(id!, lastMsgPage, 50);
  const { auth } = useAuth();
  const [showHandoff, setShowHandoff] = useState(false);
  const [showAuditModal, setShowAuditModal] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>('messages');
  const [jumpToPage, setJumpToPage] = useState<number | undefined>(undefined);
  const [showMoreMenu, setShowMoreMenu] = useState(false);

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
    return <div className="p-8 text-[var(--text-tertiary)]">Loading session...</div>;
  }

  if (error || !session) {
    return (
      <div className="p-8">
        <button onClick={() => navigate('/')} className="text-[var(--brand)] text-sm mb-4 hover:underline">
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

  const toolColor = TOOL_COLORS[session.source_tool] || '#6B7280';

  // Quick preview data
  const previewData = (() => {
    const messages = lastMessagesData?.messages;
    if (!messages || messages.length === 0) return null;
    const last3 = messages.slice(-3).map((m) => {
      const role = String(m.role || 'unknown');
      let text = '';
      if (typeof m.content === 'string') {
        text = m.content;
      } else if (Array.isArray(m.content)) {
        const textBlock = m.content.find(
          (b: unknown) => typeof b === 'object' && b !== null && (b as Record<string, unknown>).type === 'text',
        ) as Record<string, unknown> | undefined;
        if (textBlock) text = String(textBlock.text || '');
      } else if (m.messages_text) {
        text = String(m.messages_text);
      }
      return { role, text: text.slice(0, 120) + (text.length > 120 ? '...' : '') };
    });
    return last3;
  })();

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Back link */}
      <div className="px-4 pt-3">
        <button
          onClick={() => navigate('/')}
          className="text-[var(--brand)] text-sm hover:underline inline-flex items-center gap-1"
        >
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="15 18 9 12 15 6" />
          </svg>
          Back to Sessions
        </button>
      </div>

      {/* Header card */}
      <div className="mx-4 mt-2 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-sm)]">
        {/* Top row: tool + actions */}
        <div className="px-5 pt-4 flex items-start justify-between">
          <div className="flex items-center gap-3">
            <span
              className="w-6 h-6 rounded-full shrink-0"
              style={{ backgroundColor: toolColor }}
            />
            <span className="text-base font-semibold text-[var(--text-primary)]">
              {fullToolName(session.source_tool)}
            </span>
            {session.alias && (
              <span className="text-sm text-purple-400 font-mono">
                {session.alias}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowHandoff(true)}
              className="px-5 py-2.5 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors"
            >
              Hand Off
            </button>
            <div className="relative">
              <button
                onClick={() => setShowMoreMenu(!showMoreMenu)}
                className="p-1.5 text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] rounded-lg transition-colors"
              >
                <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
                  <circle cx="12" cy="5" r="2" />
                  <circle cx="12" cy="12" r="2" />
                  <circle cx="12" cy="19" r="2" />
                </svg>
              </button>
              {showMoreMenu && (
                <MoreMenu
                  sessionId={session.id}
                  sourceTool={session.source_tool}
                  onRunAudit={() => { setShowMoreMenu(false); setShowAuditModal(true); }}
                  onEditAlias={() => { setShowMoreMenu(false); handleAliasEdit(); }}
                  onDelete={() => {
                    setShowMoreMenu(false);
                    if (confirm('Delete this session? This cannot be undone.')) {
                      auth!.client.deleteSession(session.id).then(() => navigate('/'));
                    }
                  }}
                  onClose={() => setShowMoreMenu(false)}
                />
              )}
            </div>
          </div>
        </div>

        {/* Alias editing */}
        {editingAlias && (
          <div className="px-5 mt-2 flex items-center gap-2">
            <input
              type="text"
              value={aliasInput}
              onChange={(e) => setAliasInput(e.target.value)}
              onKeyDown={handleAliasKeyDown}
              onBlur={handleAliasSave}
              autoFocus
              placeholder="e.g. auth-debug"
              className="w-48 px-2 py-1 text-sm bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)]"
            />
            {aliasError && (
              <span className="text-red-400 text-xs">{aliasError}</span>
            )}
          </div>
        )}

        {/* Title */}
        <div className="px-5 mt-2">
          <h1 className="text-2xl font-semibold text-[var(--text-primary)] break-words leading-snug">
            {session.title || 'Untitled session'}
          </h1>
        </div>

        {/* Metadata pills */}
        <div className="px-5 mt-2 flex flex-wrap items-center gap-2 text-[13px]">
          <span className="font-mono text-[var(--text-tertiary)]">{session.id}</span>
          {session.model_id && session.model_id !== '<synthetic>' && (
            <>
              <span className="text-[var(--text-tertiary)]">&middot;</span>
              <span className="text-[var(--text-secondary)]">{abbreviateModel(session.model_id)}</span>
            </>
          )}
          <span className="text-[var(--text-tertiary)]">&middot;</span>
          <span className="text-[var(--text-secondary)] tabular-nums">{session.message_count} msgs</span>
          {totalTokens > 0 && (
            <>
              <span className="text-[var(--text-tertiary)]">&middot;</span>
              <span className="text-[var(--text-secondary)] tabular-nums">{formatTokens(totalTokens)} tokens</span>
            </>
          )}
          {durationStr && (
            <>
              <span className="text-[var(--text-tertiary)]">&middot;</span>
              <span className="text-[var(--text-secondary)]">{durationStr}</span>
            </>
          )}
          {cost > 0 && (
            <>
              <span className="text-[var(--text-tertiary)]">&middot;</span>
              <span className="text-[var(--text-secondary)]">${cost.toFixed(4)}</span>
            </>
          )}
          <span className="text-[var(--text-tertiary)]">&middot;</span>
          <span className="text-[var(--text-tertiary)]">
            <RelativeDate iso={session.updated_at} />
          </span>
        </div>

        {/* Project link */}
        {session.git_remote_normalized && (
          <div className="px-5 mt-2">
            <Link
              to={`/projects/${encodeURIComponent(session.git_remote_normalized)}`}
              className="text-sm text-[var(--brand)] hover:underline inline-flex items-center gap-1"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
              </svg>
              {session.git_remote_normalized}
            </Link>
          </div>
        )}

        {/* Branch + tags row */}
        {(session.parent_session_id || session.tags.length > 0) && (
          <div className="px-5 mt-2 flex flex-wrap items-center gap-2">
            {session.parent_session_id && (
              <span className="text-sm text-[var(--text-tertiary)]">
                Forked from{' '}
                <a href={`/sessions/${session.parent_session_id}`} className="text-[var(--brand)] hover:underline font-mono">
                  {session.parent_session_id.slice(0, 16)}
                </a>
              </span>
            )}
            {session.tags.map((t) => (
              <span key={t} className="px-1.5 py-0.5 text-xs bg-[var(--bg-tertiary)] border border-[var(--border)] rounded text-[var(--text-secondary)]">
                {t}
              </span>
            ))}
          </div>
        )}

        {/* CLI commands (compact) */}
        <div className="px-5 mt-3 flex flex-wrap items-center gap-3 text-xs">
          <div className="flex items-center gap-1.5">
            <code className="text-[var(--text-tertiary)] bg-[var(--bg-primary)] px-2 py-0.5 rounded font-mono">
              sfs resume {session.id}
            </code>
            <CopyButton text={`sfs resume ${session.id}`} label="Copy" />
          </div>
          <div className="flex items-center gap-1.5">
            <code className="text-[var(--text-tertiary)] bg-[var(--bg-primary)] px-2 py-0.5 rounded font-mono">
              sfs show {session.id}
            </code>
            <CopyButton text={`sfs show ${session.id}`} label="Copy" />
          </div>
        </div>

        {/* Audit status bar */}
        {auditReport && (
          <div className="px-5 mt-3 flex items-center gap-3">
            <AuditScoreBar score={auditReport.summary.trust_score} />
            <span className="text-xs text-[var(--text-tertiary)]">
              Audited <RelativeDate iso={auditReport.timestamp} /> ({auditReport.model})
            </span>
          </div>
        )}

        {/* Quick preview */}
        {previewData && previewData.length > 0 && (
          <div className="px-5 mt-3 pb-1">
            <div className="border-t border-[var(--border)] pt-3">
              <div className="space-y-1">
                {previewData.map((m, i) => (
                  <div key={i} className="flex gap-2 text-sm">
                    <span className={`shrink-0 text-xs font-medium px-1.5 py-0.5 rounded ${
                      m.role === 'assistant'
                        ? 'bg-green-500/10 text-green-400'
                        : m.role === 'user'
                          ? 'bg-blue-500/10 text-blue-400'
                          : 'bg-gray-500/10 text-gray-400'
                    }`}>
                      {m.role === 'assistant' ? 'AI' : m.role === 'user' ? 'You' : m.role}
                    </span>
                    <span className="text-[var(--text-tertiary)] truncate">{m.text || '(no text)'}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Bookmarks section */}
        <BookmarksSection sessionId={session.id} />

        {/* Tabs */}
        <div className="flex px-5 mt-2 border-t border-[var(--border)]">
          {(['messages', 'summary', 'audit'] as Tab[]).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-3 text-[14px] font-medium transition-colors relative ${
                activeTab === tab
                  ? 'text-[var(--brand)]'
                  : 'text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
              }`}
            >
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
              {activeTab === tab && (
                <span className="absolute bottom-0 left-4 right-4 h-0.5 bg-[var(--brand)] rounded-full" />
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto">
        {activeTab === 'messages' && <ConversationView sessionId={session.id} initialPage={jumpToPage} />}
        {activeTab === 'summary' && <SummaryTab sessionId={session.id} />}
        {activeTab === 'audit' && (
          <AuditTab sessionId={session.id} messageCount={session.message_count} sessionTitle={session.title || undefined} onJumpToMessage={handleJumpToMessage} />
        )}
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

function MoreMenu({
  sessionId,
  sourceTool,
  onRunAudit,
  onEditAlias,
  onDelete,
  onClose,
}: {
  sessionId: string;
  sourceTool: string;
  onRunAudit: () => void;
  onEditAlias: () => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const resumeCmd = `sfs resume ${sessionId} --in ${sourceTool}`;

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 z-30" onClick={onClose} />
      <div className="absolute right-0 top-10 z-40 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-lg shadow-[var(--shadow-md)] py-1 min-w-[180px]">
        <button
          onClick={() => {
            navigator.clipboard.writeText(resumeCmd).then(() => {
              setCopied(true);
              setTimeout(() => { setCopied(false); onClose(); }, 800);
            });
          }}
          className="w-full text-left px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] transition-colors"
        >
          {copied ? 'Copied!' : `Resume in ${fullToolName(sourceTool)}`}
        </button>
        <button
          onClick={onRunAudit}
          className="w-full text-left px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] transition-colors"
        >
          Run Audit
        </button>
        <button
          onClick={onEditAlias}
          className="w-full text-left px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] transition-colors"
        >
          Edit Alias
        </button>
        <div className="border-t border-[var(--border)] my-1" />
        <button
          onClick={onDelete}
          className="w-full text-left px-3 py-2 text-sm text-red-400 hover:bg-red-500/10 transition-colors"
        >
          Delete Session
        </button>
      </div>
    </>
  );
}

function BookmarksSection({ sessionId }: { sessionId: string }) {
  const { data: foldersData } = useFolders();
  const addBookmark = useAddBookmark();
  const [showAdd, setShowAdd] = useState(false);

  const folders = foldersData?.folders ?? [];

  if (folders.length === 0) return null;

  return (
    <div className="px-5 mt-2">
      {!showAdd ? (
        <button
          onClick={() => setShowAdd(true)}
          className="text-xs text-[var(--brand)] hover:underline"
        >
          + Add to folder
        </button>
      ) : (
        <div className="flex flex-wrap items-center gap-1.5">
          {folders.map((f) => (
            <button
              key={f.id}
              onClick={() => {
                addBookmark.mutate({ folderId: f.id, sessionId }, {
                  onSettled: () => setShowAdd(false),
                });
              }}
              className="inline-flex items-center gap-1.5 px-2 py-1 text-xs text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] rounded transition-colors border border-[var(--border)]"
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
            className="text-xs text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] px-1"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function AuditScoreBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color =
    pct >= 90 ? 'bg-green-500' : pct >= 70 ? 'bg-yellow-500' : 'bg-red-500';
  const textColor =
    pct >= 90 ? 'text-green-400' : pct >= 70 ? 'text-yellow-400' : 'text-red-400';

  return (
    <div className="flex items-center gap-2">
      <span className={`text-sm font-semibold tabular-nums ${textColor}`}>{pct}%</span>
      <div className="w-24 h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
