import { useState, useCallback, useMemo } from 'react';
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
import DeleteScopeDialog from './DeleteScopeDialog';
import type { DeleteScope } from './DeleteScopeDialog';
import { useDeleteSession } from '../hooks/useSessions';
import { useToast } from '../hooks/useToast';
import { TOOL_COLORS } from '../utils/tools';
import { Button } from '../components/ui/Button';
import { Card } from '../components/ui/Card';
import { Tabs } from '../components/ui/Tabs';
import { Dropdown } from '../components/ui/Dropdown';

type Tab = 'messages' | 'summary' | 'audit';

const TAB_DEFS: Array<{ key: Tab; label: string }> = [
  { key: 'messages', label: 'Messages' },
  { key: 'summary', label: 'Summary' },
  { key: 'audit', label: 'Audit' },
];

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: session, isLoading, error, refetch } = useSession(id!);
  const { data: auditReport } = useAudit(id!);

  // Fetch newest messages for Quick Preview (page 1 newest-first = most recent)
  const { data: lastMessagesData } = useMessages(id!, 1, 50, 'newest');
  const { auth } = useAuth();
  const [showHandoff, setShowHandoff] = useState(false);
  const [showAuditModal, setShowAuditModal] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>('messages');
  const [jumpToPage, setJumpToPage] = useState<number | undefined>(undefined);
  const deleteSession = useDeleteSession();
  const { addToast } = useToast();

  function handleJumpToMessage(messageIndex: number) {
    const page = Math.floor(messageIndex / 50) + 1;
    setJumpToPage(page);
    setActiveTab('messages');
    // Clear after a tick so ConversationView picks it up then resets
    setTimeout(() => setJumpToPage(undefined), 100);
  }
  const [editingMeta, setEditingMeta] = useState(false);
  const [titleInput, setTitleInput] = useState('');
  const [aliasInput, setAliasInput] = useState('');
  const [metaError, setMetaError] = useState<string | null>(null);

  const handleMetaEdit = useCallback(() => {
    setTitleInput(session?.title || '');
    setAliasInput(session?.alias || '');
    setMetaError(null);
    setEditingMeta(true);
  }, [session?.title, session?.alias]);

  const handleMetaSave = useCallback(async () => {
    if (!auth || !session) return;
    const trimmedTitle = titleInput.trim();
    const trimmedAlias = aliasInput.trim();
    const prevTitle = session.title || '';
    const prevAlias = session.alias || '';
    const titleChanged = trimmedTitle !== prevTitle;
    const aliasChanged = trimmedAlias !== prevAlias;
    if (!titleChanged && !aliasChanged) {
      setEditingMeta(false);
      return;
    }
    if (titleChanged && !trimmedTitle) {
      setMetaError('Title cannot be empty');
      return;
    }
    try {
      if (aliasChanged && !trimmedAlias) {
        await auth.client.clearAlias(session.id);
        if (titleChanged) {
          await auth.client.updateSession(session.id, { title: trimmedTitle });
        }
      } else {
        const body: { title?: string; alias?: string } = {};
        if (titleChanged) body.title = trimmedTitle;
        if (aliasChanged) body.alias = trimmedAlias;
        await auth.client.updateSession(session.id, body);
      }
      setEditingMeta(false);
      setMetaError(null);
      refetch();
    } catch (err) {
      setMetaError(String(err));
    }
  }, [auth, session, titleInput, aliasInput, refetch]);

  const handleMetaKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter') handleMetaSave();
      if (e.key === 'Escape') setEditingMeta(false);
    },
    [handleMetaSave],
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

  const toolColor = TOOL_COLORS[session.source_tool] || 'var(--text-tertiary)';

  // More-menu: resume command + items
  const captureOnly = ['cursor', 'cline', 'roo-code', 'amp'].includes(session.source_tool);
  const resumeTool = captureOnly ? 'claude-code' : session.source_tool;
  const resumeCmd = `sfs resume ${session.id} --in ${resumeTool}`;

  const moreMenuItems = useMemo(() => [
    { key: 'resume', label: `Resume in ${fullToolName(resumeTool)}` },
    { key: 'audit', label: 'Run Audit' },
    { key: 'rename', label: 'Rename (Title & Alias)' },
    { key: 'sep', label: '', separator: true as const },
    { key: 'delete', label: 'Delete Session', danger: true as const },
  ], [resumeTool]);

  function handleMoreSelect(key: string) {
    switch (key) {
      case 'resume':
        navigator.clipboard.writeText(resumeCmd);
        break;
      case 'audit':
        setShowAuditModal(true);
        break;
      case 'rename':
        handleMetaEdit();
        break;
      case 'delete':
        setShowDeleteDialog(true);
        break;
    }
  }

  // Quick preview data
  const previewData = (() => {
    const messages = lastMessagesData?.messages;
    if (!messages || messages.length === 0) return null;
    const last3 = messages.slice(0, 3).map((m) => {
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

      {/* Header card — tool-colored left edge per Phase 1b card pattern */}
      <Card level="elevated" toolEdge={toolColor} className="mx-4 mt-2">
        {/* Top row: tool name + actions */}
        <div className="px-5 pt-4 flex items-start justify-between">
          <div className="flex items-center gap-3">
            <span className="text-base font-semibold text-[var(--text-primary)]">
              {fullToolName(session.source_tool)}
            </span>
            {session.alias && (
              <span className="text-sm text-[var(--brand)] font-mono">
                {session.alias}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button onClick={() => setShowHandoff(true)} size="sm">
              Hand Off
            </Button>
            <Dropdown
              trigger={
                <button className="p-1.5 text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] rounded-lg transition-colors" aria-label="Session actions">
                  <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
                    <circle cx="12" cy="5" r="2" />
                    <circle cx="12" cy="12" r="2" />
                    <circle cx="12" cy="19" r="2" />
                  </svg>
                </button>
              }
              items={moreMenuItems}
              onSelect={handleMoreSelect}
              menuLabel="Session actions"
            />
          </div>
        </div>

        {/* Title + alias editing — side by side */}
        {editingMeta ? (
          <div className="px-5 mt-2">
            <div className="flex items-start gap-3 flex-wrap">
              <label className="flex-1 min-w-[12rem]">
                <span className="block text-[11px] uppercase tracking-[0.02em] text-[var(--text-tertiary)] mb-1">Title</span>
                <input
                  type="text"
                  value={titleInput}
                  onChange={(e) => setTitleInput(e.target.value)}
                  onKeyDown={handleMetaKeyDown}
                  autoFocus
                  placeholder="Session title"
                  aria-label="Session title"
                  className="w-full px-2 py-1 text-base bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                />
              </label>
              <label className="w-56">
                <span className="block text-[11px] uppercase tracking-[0.02em] text-[var(--text-tertiary)] mb-1">Alias</span>
                <input
                  type="text"
                  value={aliasInput}
                  onChange={(e) => setAliasInput(e.target.value)}
                  onKeyDown={handleMetaKeyDown}
                  placeholder="e.g. auth-debug"
                  aria-label="Session alias"
                  className="w-full px-2 py-1 text-sm bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                />
              </label>
              <div className="flex items-center gap-2 mt-5">
                <Button onClick={handleMetaSave} variant="primary" size="sm">
                  Save
                </Button>
                <Button onClick={() => setEditingMeta(false)} variant="ghost" size="sm">
                  Cancel
                </Button>
              </div>
            </div>
            {metaError && (
              <div className="text-[var(--danger)] text-xs mt-1">{metaError}</div>
            )}
          </div>
        ) : (
          <div className="px-5 mt-2 group">
            <h1
              onClick={handleMetaEdit}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleMetaEdit(); } }}
              aria-label="Edit title and alias"
              title="Click to rename title and alias"
              className="text-2xl font-semibold text-[var(--text-primary)] break-words leading-snug cursor-text hover:underline decoration-[var(--text-tertiary)] decoration-dotted underline-offset-4"
            >
              {session.title || 'Untitled session'}
            </h1>
          </div>
        )}

        {/* Metadata row — session ID as mono-chip */}
        <div className="px-5 mt-2 flex flex-wrap items-center gap-2 text-[13px]">
          <span className="text-mono-chip">{session.id}</span>
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
            <code className="text-[var(--text-tertiary)] bg-[var(--bg-sunken)] px-2 py-0.5 rounded font-mono">
              sfs resume {session.id}
            </code>
            <CopyButton text={`sfs resume ${session.id}`} label="Copy" />
          </div>
          <div className="flex items-center gap-1.5">
            <code className="text-[var(--text-tertiary)] bg-[var(--bg-sunken)] px-2 py-0.5 rounded font-mono">
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

        {/* DLP Scan Results */}
        <DLPScanSection session={session} />

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

        {/* Tabs — bare mode: tab bar only, content lives in scrollable container below */}
        <div className="px-5 mt-2">
          <Tabs
            tabs={TAB_DEFS}
            activeKey={activeTab}
            onChange={(key) => setActiveTab(key as Tab)}
            bare
          />
        </div>
      </Card>

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
      <AuditModal
        open={showAuditModal}
        sessionId={session.id}
        messageCount={session.message_count}
        onClose={() => setShowAuditModal(false)}
        onComplete={() => setShowAuditModal(false)}
      />
      {showDeleteDialog && (
        <DeleteScopeDialog
          count={1}
          isPending={deleteSession.isPending}
          onCancel={() => setShowDeleteDialog(false)}
          onConfirm={(scope: DeleteScope) => {
            deleteSession.mutate(
              { id: session.id, scope },
              {
                onSuccess: () => {
                  addToast('success', 'Session deleted');
                  navigate('/');
                },
                onError: (err) => {
                  addToast('error', `Delete failed: ${String(err)}`);
                  setShowDeleteDialog(false);
                },
              },
            );
          }}
        />
      )}
    </div>
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

interface DLPScanResult {
  findings_count: number;
  finding_types: string[];
  action_taken: string;
  mode: string;
  scanned_at: string;
  categories_scanned?: string[];
}

function DLPScanSection({ session }: { session: object }) {
  const raw = (session as { dlp_scan_results?: string | DLPScanResult | null }).dlp_scan_results;
  if (!raw) return null;

  let scanResult: DLPScanResult;
  try {
    scanResult = typeof raw === 'string' ? JSON.parse(raw) : raw as DLPScanResult;
  } catch {
    return null;
  }

  if (!scanResult.findings_count || scanResult.findings_count === 0) return null;

  const actionLabel =
    scanResult.action_taken === 'redact' ? 'Redacted'
    : scanResult.action_taken === 'block' ? 'Blocked'
    : scanResult.action_taken === 'warn' ? 'Warned'
    : 'Scanned';

  const timeAgo = scanResult.scanned_at
    ? (() => {
        const diff = Date.now() - new Date(scanResult.scanned_at).getTime();
        if (diff < 60000) return 'just now';
        if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
        if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
        return `${Math.floor(diff / 86400000)}d ago`;
      })()
    : null;

  return (
    <div className="px-5 mt-3">
      <div className="border border-red-500/20 bg-red-500/5 rounded-lg p-3">
        <div className="flex items-center gap-2 mb-2">
          <svg className="w-4 h-4 text-red-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
          <span className="text-sm font-semibold text-[var(--text-primary)]">DLP Scan</span>
        </div>
        <div className="text-xs text-[var(--text-tertiary)] mb-2">
          {scanResult.findings_count} finding{scanResult.findings_count !== 1 ? 's' : ''}
          {' · '}{actionLabel}
          {timeAgo && <>{' · '}Scanned {timeAgo}</>}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {scanResult.finding_types.map((type) => (
            <span key={type} className="px-2 py-0.5 text-xs font-mono bg-red-500/10 text-red-500 rounded">
              {type}
            </span>
          ))}
        </div>
      </div>
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
        <div className={`h-full rounded-full transition-[width] duration-300 ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
