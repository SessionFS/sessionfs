import { useState, useMemo } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useAudit } from '../hooks/useAudit';
import AuditModal from './AuditModal';
import type { AuditFinding, AuditReport } from '../api/client';
import {
  exportAuditJson,
  exportAuditMarkdown,
  exportAuditCsv,
  downloadFile,
} from '../utils/auditExport';
import { useToast } from '../hooks/useToast';
import { Button } from '../components/ui/Button';
import { Card } from '../components/ui/Card';
import Skeleton from '../components/Skeleton';
import { Dropdown } from '../components/ui/Dropdown';

interface Props {
  sessionId: string;
  messageCount: number;
  sessionTitle?: string;
  onJumpToMessage?: (messageIndex: number) => void;
}

/* ── Dismiss state (local only, will be persisted via API later) ── */
interface DismissInfo {
  reason: string;
  timestamp: number;
}

function useDismissState() {
  const [dismissed, setDismissed] = useState<Record<string, DismissInfo>>({});

  const dismiss = (key: string, reason: string) => {
    setDismissed(prev => ({ ...prev, [key]: { reason, timestamp: Date.now() } }));
  };

  const restore = (key: string) => {
    setDismissed(prev => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const isDismissed = (key: string) => key in dismissed;

  return { dismissed, dismiss, restore, isDismissed };
}

function findingKey(f: AuditFinding, idx: number): string {
  return `${f.message_index}-${idx}`;
}


export default function AuditTab({ sessionId, messageCount, sessionTitle, onJumpToMessage }: Props) {
  const { data: report, isLoading, error } = useAudit(sessionId);
  const [showModal, setShowModal] = useState(false);
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const is404 = error && 'status' in error && (error as { status: number }).status === 404;

  /* ── "Running" state ── */
  const isRunning = !isLoading && !error && report && 'status' in report && (report as unknown as { status: string }).status === 'accepted';

  if (isRunning) {
    const estimatedSeconds = Math.max(5, Math.round(messageCount * 0.5));
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <div className="bg-bg-secondary border border-border rounded-lg p-6 text-center max-w-sm">
          <div className="w-8 h-8 border-2 border-accent border-t-transparent rounded-full animate-spin mx-auto mb-4" />
          <p className="text-text-primary font-medium mb-1">Audit in progress…</p>
          <p className="text-text-muted text-xs mb-4">
            Estimated time: ~{estimatedSeconds}s ({messageCount} messages)
          </p>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => queryClient.invalidateQueries({ queryKey: ['audit', sessionId] })}
          >
            Refresh
          </Button>
        </div>
      </div>
    );
  }

  if (!isLoading && (is404 || (!report && !error))) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <svg className="w-10 h-10 text-[var(--text-tertiary)] mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 12l2 2 4-4m6 2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z" />
        </svg>
        <p className="text-[15px] font-semibold text-[var(--text-primary)] mb-1">No audit yet</p>
        <p className="text-[13px] text-[var(--text-tertiary)] mb-4">
          Auditing evaluates factual claims in the conversation against tool output evidence.
        </p>
        <Button onClick={() => setShowModal(true)}>
          Run Audit
        </Button>
        <AuditModal open={showModal} sessionTitle={sessionTitle} sessionId={sessionId} messageCount={messageCount}
          onClose={() => setShowModal(false)} onComplete={() => { setShowModal(false); addToast('info', 'Audit started'); }} />
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="py-16 px-4">
        <Skeleton type="card" />
      </div>
    );
  }

  if (error && !is404) {
    return <div className="p-4 text-red-400 text-sm">Failed to load audit: {String(error)}</div>;
  }

  if (!report) return null;

  return (
    <AuditReportView
      report={report}
      sessionId={sessionId}
      messageCount={messageCount}
      sessionTitle={sessionTitle}
      onJumpToMessage={onJumpToMessage}
      showModal={showModal}
      setShowModal={setShowModal}
    />
  );
}


/* ── Report view (extracted to use hooks freely) ── */

function AuditReportView({
  report,
  sessionId,
  messageCount,
  sessionTitle,
  onJumpToMessage,
  showModal,
  setShowModal,
}: {
  report: AuditReport;
  sessionId: string;
  messageCount: number;
  sessionTitle?: string;
  onJumpToMessage?: (idx: number) => void;
  showModal: boolean;
  setShowModal: (v: boolean) => void;
}) {
  const { dismissed, dismiss, restore, isDismissed } = useDismissState();
  const { addToast } = useToast();
  const [minConfidence, setMinConfidence] = useState(0);

  const contradictions = report.findings.filter(f => f.verdict === 'hallucination');
  const unverified = report.findings.filter(f => f.verdict === 'unverified');
  const verified = report.findings.filter(f => f.verdict === 'verified');
  const pct = Math.round(report.summary.trust_score * 100);

  /* ── Derived metrics ── */
  const avgConfidence = useMemo(() => {
    const withConf = report.findings.filter(f => f.confidence != null);
    if (withConf.length === 0) return null;
    return Math.round(withConf.reduce((s, f) => s + (f.confidence ?? 0), 0) / withConf.length);
  }, [report.findings]);

  const uniqueCweCount = useMemo(() => {
    const cwes = new Set(report.findings.map(f => f.cwe_id).filter(Boolean));
    return cwes.size;
  }, [report.findings]);

  /* ── Filter and partition findings ── */
  const filterByConfidence = (findings: AuditFinding[]) =>
    findings.filter(f => (f.confidence ?? 100) >= minConfidence);

  const partitionDismissed = (findings: AuditFinding[], baseIdx: number) => {
    const active: { finding: AuditFinding; key: string }[] = [];
    const hidden: { finding: AuditFinding; key: string }[] = [];
    findings.forEach((f, i) => {
      const key = findingKey(f, baseIdx + i);
      if (isDismissed(key)) hidden.push({ finding: f, key });
      else active.push({ finding: f, key });
    });
    return { active, hidden };
  };

  const filteredContradictions = filterByConfidence(contradictions);
  const filteredUnverified = filterByConfidence(unverified);
  const filteredVerified = filterByConfidence(verified);

  const { active: activeContradictions, hidden: hiddenContradictions } = partitionDismissed(filteredContradictions, 0);
  const { active: activeUnverified, hidden: hiddenUnverified } = partitionDismissed(filteredUnverified, 1000);
  const { active: activeVerified, hidden: hiddenVerified } = partitionDismissed(filteredVerified, 2000);

  const totalDismissed = hiddenContradictions.length + hiddenUnverified.length + hiddenVerified.length;

  return (
    <div className="p-4 max-w-3xl mx-auto">
      {/* Warnings banner */}
      {report.warnings && report.warnings.length > 0 && (
        <div className="bg-yellow-500/10 border border-yellow-500/20 rounded-lg p-3 mb-4">
          <p className="text-yellow-400 text-sm font-medium">Warning</p>
          {report.warnings.map((w, i) => (
            <p key={i} className="text-text-muted text-sm mt-1">{w}</p>
          ))}
        </div>
      )}

      {/* Metric Cards */}
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-3 mb-6">
        <MetricCard
          label="Trust Score"
          value={`${pct}%`}
          color={pct >= 85 ? 'text-green-400' : pct >= 60 ? 'text-yellow-400' : 'text-red-400'}
        />
        <MetricCard label="Claims" value={String(report.summary.total_claims)} color="text-text-secondary" />
        <MetricCard label="Contradictions" value={String(contradictions.length)} color={contradictions.length > 0 ? 'text-red-400' : 'text-green-400'} />
        <MetricCard label="Unverified" value={String(unverified.length)} color="text-text-muted" />
        {avgConfidence !== null && (
          <MetricCard label="Confidence" value={`${avgConfidence}%`} color={avgConfidence >= 80 ? 'text-green-400' : avgConfidence >= 50 ? 'text-yellow-400' : 'text-red-400'} />
        )}
        {uniqueCweCount > 0 && (
          <MetricCard label="CWE IDs" value={String(uniqueCweCount)} color="text-orange-400" />
        )}
      </div>

      {/* Confidence filter + badges + actions */}
      <div className="flex items-center gap-2 mb-6 flex-wrap">
        <div className="flex items-center gap-2 mr-2">
          <label className="text-xs text-text-muted whitespace-nowrap">Min confidence:</label>
          <input
            type="range"
            min={0}
            max={100}
            value={minConfidence}
            onChange={e => setMinConfidence(Number(e.target.value))}
            className="w-20 h-1 accent-accent"
          />
          <span className="text-xs text-text-secondary tabular-nums w-7">{minConfidence}%</span>
        </div>
        {contradictions.length > 0 && (
          <span className="px-2 py-0.5 text-xs rounded-full bg-red-500/20 text-red-400">{contradictions.length} contradictions</span>
        )}
        {unverified.length > 0 && (
          <span className="px-2 py-0.5 text-xs rounded-full bg-yellow-500/20 text-yellow-400">{unverified.length} unverified</span>
        )}
        {verified.length > 0 && (
          <span className="px-2 py-0.5 text-xs rounded-full bg-green-500/20 text-green-400">{verified.length} verified</span>
        )}
        <div className="flex-1" />
        <Button variant="secondary" size="sm" onClick={() => setShowModal(true)}>
          Re-audit
        </Button>
        <DownloadDropdown report={report} sessionTitle={sessionTitle} />
      </div>

      {/* Contradictions */}
      {activeContradictions.length > 0 && (
        <section className="mb-6">
          <h3 className="text-sm font-medium text-red-400 mb-1">Contradictions</h3>
          <p className="text-xs text-text-muted mb-3">Claims that directly contradict tool outputs</p>
          <div className="flex flex-col gap-2">
            {activeContradictions.map(({ finding, key }) => (
              <FindingCard key={key} finding={finding} findingKey={key} onJump={onJumpToMessage} onDismiss={dismiss} />
            ))}
          </div>
        </section>
      )}

      {contradictions.length === 0 && report.summary.total_claims > 0 && (
        <div className="mb-6 p-3 bg-green-500/10 border border-green-500/20 rounded-lg text-sm text-green-400">
          No contradictions found across {report.summary.total_claims} claims.
        </div>
      )}

      {/* Unverified */}
      {activeUnverified.length > 0 && (
        <CollapsibleSection title={`${activeUnverified.length} unverified claims`} defaultOpen={false}>
          <div className="flex flex-col gap-2">
            {activeUnverified.map(({ finding, key }) => (
              <FindingCard key={key} finding={finding} findingKey={key} onJump={onJumpToMessage} onDismiss={dismiss} />
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* Verified */}
      {activeVerified.length > 0 && (
        <CollapsibleSection title={`${activeVerified.length} verified claims`} defaultOpen={false}>
          <div className="flex flex-col gap-2">
            {activeVerified.map(({ finding, key }) => (
              <FindingCard key={key} finding={finding} findingKey={key} onJump={onJumpToMessage} onDismiss={dismiss} />
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* Dismissed findings */}
      {totalDismissed > 0 && (
        <CollapsibleSection title={`Dismissed`} defaultOpen={false} badge={`${totalDismissed} dismissed`}>
          <div className="flex flex-col gap-2">
            {[...hiddenContradictions, ...hiddenUnverified, ...hiddenVerified].map(({ finding, key }) => (
              <div key={key} className="opacity-60">
                <div className="border border-border rounded-lg p-3 line-through">
                  <div className="flex items-center gap-2">
                    <VerdictBadge verdict={finding.verdict} />
                    <SeverityBadge severity={finding.severity} />
                    <span className="text-sm text-text-muted truncate flex-1">{finding.claim}</span>
                    <button
                      onClick={() => restore(key)}
                      className="px-2 py-0.5 text-xs text-accent border border-accent/30 rounded hover:bg-accent/10 transition-colors shrink-0"
                    >
                      Restore
                    </button>
                  </div>
                  {dismissed[key] && (
                    <p className="text-xs text-text-muted mt-1 no-underline" style={{ textDecoration: 'none' }}>
                      Reason: {dismissed[key].reason}
                    </p>
                  )}
                </div>
              </div>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* Audit meta */}
      <div className="mt-6 pt-4 border-t border-border flex gap-4 text-xs text-text-muted">
        <span>Model: {report.model}</span>
        <span>{new Date(report.timestamp).toLocaleString()}</span>
      </div>

      {/* History */}
      <AuditHistory sessionId={sessionId} />

      <AuditModal open={showModal} sessionTitle={sessionTitle} sessionId={sessionId} messageCount={messageCount}
        onClose={() => setShowModal(false)} onComplete={() => { setShowModal(false); addToast('info', 'Audit started'); }} />
    </div>
  );
}


/* ── FindingCard ── */

const SEVERITY_STYLES: Record<string, string> = {
  critical: 'bg-red-500/20 text-red-400',
  high: 'bg-orange-500/20 text-orange-400',
  low: 'bg-bg-tertiary text-text-muted',
  major: 'bg-red-500/20 text-red-400',
  moderate: 'bg-orange-500/20 text-orange-400',
  minor: 'bg-bg-tertiary text-text-muted',
};

const VERDICT_STYLES: Record<string, string> = {
  verified: 'border-green-500/40 bg-green-500/5',
  hallucination: 'border-red-500/40 bg-red-500/5',
  unverified: 'border-yellow-500/40 bg-yellow-500/5',
};

const VERDICT_BADGE_STYLES: Record<string, string> = {
  verified: 'bg-green-500/20 text-green-400',
  hallucination: 'bg-red-500/20 text-red-400',
  unverified: 'bg-yellow-500/20 text-yellow-400',
};

const CATEGORY_LABELS: Record<string, string> = {
  test_result: 'Test result',
  file_existence: 'File existence',
  command_output: 'Command output',
  data_misread: 'Data misread',
  code_claim: 'Code claim',
  dependency: 'Dependency',
  other: 'Other',
};

function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span className={`px-2 py-0.5 text-[10px] rounded uppercase font-medium ${SEVERITY_STYLES[severity] || SEVERITY_STYLES.low}`}>
      {severity}
    </span>
  );
}

function VerdictBadge({ verdict }: { verdict: string }) {
  return (
    <span className={`px-2 py-0.5 text-xs rounded font-medium ${VERDICT_BADGE_STYLES[verdict] || 'bg-bg-tertiary text-text-muted'}`}>
      {verdict}
    </span>
  );
}

function ConfidenceBar({ confidence }: { confidence: number }) {
  const color = confidence >= 80 ? 'bg-green-400' : confidence >= 50 ? 'bg-yellow-400' : 'bg-red-400';
  return (
    <div className="flex items-center gap-1.5">
      <div className="w-12 h-1.5 bg-bg-tertiary rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${confidence}%` }} />
      </div>
      <span className="text-[10px] text-text-muted tabular-nums">{confidence}%</span>
    </div>
  );
}

function FindingCard({
  finding,
  findingKey,
  onJump,
  onDismiss,
}: {
  finding: AuditFinding;
  findingKey: string;
  onJump?: (idx: number) => void;
  onDismiss: (key: string, reason: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [dismissing, setDismissing] = useState(false);
  const [dismissReason, setDismissReason] = useState('');

  const borderStyle = VERDICT_STYLES[finding.verdict] || 'border-border';

  const handleDismiss = () => {
    if (!dismissing) {
      setDismissing(true);
      return;
    }
    onDismiss(findingKey, dismissReason || 'Dismissed without reason');
    setDismissing(false);
    setDismissReason('');
  };

  const handleConfirm = () => {
    // Confirm is a no-op for now — visual acknowledgment only
    // Could be extended to mark as confirmed in API later
  };

  return (
    <div className={`border rounded-lg overflow-hidden ${borderStyle}`}>
      {/* Header row — clickable to expand */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-bg-tertiary/50 transition-colors flex-wrap"
      >
        <SeverityBadge severity={finding.severity} />
        {finding.category && (
          <span className="px-2 py-0.5 text-[10px] rounded bg-bg-tertiary text-text-muted">
            {CATEGORY_LABELS[finding.category] || finding.category}
          </span>
        )}
        {finding.cwe_id && (
          <span className="px-2 py-0.5 text-[10px] rounded bg-bg-tertiary text-orange-400 font-mono">
            {finding.cwe_id}
          </span>
        )}
        <VerdictBadge verdict={finding.verdict} />
        {finding.confidence != null && (
          <ConfidenceBar confidence={finding.confidence} />
        )}
        <span className="text-text-muted text-xs ml-auto shrink-0">#{finding.message_index}</span>
        <svg
          className={`w-3 h-3 text-text-muted transition-transform shrink-0 ${expanded ? 'rotate-90' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
      </button>

      {/* Claim text always visible */}
      <div className="px-3 pb-2">
        <p className="text-[15px] font-medium text-[var(--text-primary)]">{finding.claim}</p>
      </div>

      {/* Expanded evidence viewer */}
      {expanded && (
        <div className="px-3 pb-3 border-t border-border/50 pt-3 space-y-3">
          {/* Side-by-side: claim+explanation | evidence */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {/* Left: claim and explanation */}
            <div>
              <span className="text-[10px] text-text-muted uppercase block mb-1">Claim & Explanation</span>
              <div className="bg-bg-tertiary border border-border rounded p-2 text-xs text-text-secondary space-y-2">
                <p className="font-medium text-text-primary">{finding.claim}</p>
                <p>{finding.explanation}</p>
              </div>
            </div>

            {/* Right: evidence */}
            <div>
              <span className="text-[10px] text-text-muted uppercase block mb-1">Evidence</span>
              {finding.evidence_snippets && finding.evidence_snippets.length > 0 ? (
                <div className="space-y-1.5">
                  {finding.evidence_snippets.map((snippet, i) => (
                    <div key={i} className="bg-bg-tertiary border border-border rounded overflow-hidden">
                      <div className="px-2 py-0.5 bg-bg-tertiary border-b border-border/50 text-[10px] text-text-muted">
                        Message #{snippet.message_index}
                      </div>
                      <pre className="px-2 py-1.5 text-[13px] text-[var(--text-tertiary)] overflow-x-auto whitespace-pre-wrap max-h-32 overflow-y-auto font-mono">
                        {snippet.text}
                      </pre>
                    </div>
                  ))}
                </div>
              ) : finding.evidence ? (
                <pre className="bg-bg-tertiary border border-border rounded px-2 py-1.5 text-[13px] text-[var(--text-tertiary)] overflow-x-auto whitespace-pre-wrap max-h-40 overflow-y-auto font-mono">
                  {finding.evidence}
                </pre>
              ) : (
                <p className="text-xs text-text-muted italic">No evidence available</p>
              )}
            </div>
          </div>

          {/* Actions row */}
          <div className="flex items-center gap-2 pt-1">
            {onJump && (
              <button onClick={() => onJump(finding.message_index)}
                className="text-accent text-xs hover:underline">
                Jump to message #{finding.message_index}
              </button>
            )}
            <div className="flex-1" />
            <button
              onClick={handleConfirm}
              className="inline-flex items-center gap-1 px-2 py-1 text-xs text-green-400 border border-green-500/30 rounded hover:bg-green-500/10 transition-colors"
              title="Confirm finding"
            >
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
              Confirm
            </button>
            <button
              onClick={handleDismiss}
              className="inline-flex items-center gap-1 px-2 py-1 text-xs text-text-muted border border-border rounded hover:bg-bg-tertiary transition-colors"
              title="Dismiss finding"
            >
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
              Dismiss
            </button>
          </div>

          {/* Inline dismiss reason input */}
          {dismissing && (
            <div className="flex items-center gap-2 pt-1">
              <input
                type="text"
                placeholder="Reason for dismissal (optional)"
                value={dismissReason}
                onChange={e => setDismissReason(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleDismiss(); }}
                className="flex-1 px-2 py-1 text-xs bg-bg-tertiary border border-border rounded text-text-primary placeholder:text-text-muted/50 outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                autoFocus
              />
              <button
                onClick={handleDismiss}
                className="px-2 py-1 text-xs bg-bg-tertiary border border-border rounded text-text-secondary hover:bg-bg-tertiary/80"
              >
                Submit
              </button>
              <button
                onClick={() => { setDismissing(false); setDismissReason(''); }}
                className="px-2 py-1 text-xs text-text-muted hover:text-text-secondary"
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


/* ── Shared components ── */

function MetricCard({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <Card className="p-3 text-center">
      <div className={`text-3xl font-bold tabular-nums ${color}`}>{value}</div>
      <div className="text-micro text-[var(--text-tertiary)] uppercase mt-0.5">{label}</div>
    </Card>
  );
}


function CollapsibleSection({ title, defaultOpen, children, badge }: { title: string; defaultOpen: boolean; children: React.ReactNode; badge?: string }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="mb-4">
      <button onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-sm text-text-muted hover:text-text-secondary mb-2 w-full text-left">
        <svg className={`w-3 h-3 transition-transform ${open ? 'rotate-90' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        {title}
        {badge && (
          <span className="px-2 py-0.5 rounded text-xs font-medium bg-bg-tertiary text-text-muted ml-1">
            {badge}
          </span>
        )}
      </button>
      {open && children}
    </section>
  );
}


function AuditHistory({ sessionId }: { sessionId: string }) {
  const { auth } = useAuth();
  const { data } = useQuery({
    queryKey: ['auditHistory', sessionId],
    queryFn: () => auth!.client.getAuditHistory(sessionId).catch(() => []),
    enabled: !!auth,
    staleTime: 60_000,
  });

  const history = data as { id: string; judge_model: string; trust_score: number; total_claims: number; contradiction_count: number; created_at: string }[] | undefined;

  if (!history || history.length <= 1) return null;

  return (
    <section className="mt-6">
      <h3 className="text-sm text-text-muted mb-2">Audit History</h3>
      <div className="space-y-1">
        {history.map((h) => {
          const pct = h.trust_score;
          const color = pct >= 85 ? 'text-green-400' : pct >= 60 ? 'text-yellow-400' : 'text-red-400';
          return (
            <div key={h.id} className="flex items-center gap-3 text-xs text-text-muted py-1 border-b border-border/50 last:border-0">
              <span className="w-24">{new Date(h.created_at).toLocaleDateString()}</span>
              <span className="w-20 truncate">{h.judge_model}</span>
              <span className="w-12 text-right">{h.total_claims} claims</span>
              <span className={`w-10 text-right font-medium ${color}`}>{pct}%</span>
              <span className="w-16 text-right">{h.contradiction_count > 0 ? `${h.contradiction_count} issues` : 'clean'}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}


function DownloadDropdown({ report, sessionTitle }: { report: AuditReport; sessionTitle?: string }) {
  const base = `audit-${report.session_id.slice(0, 12)}`;

  const items = useMemo(() => [
    { key: 'json', label: 'JSON' },
    { key: 'md', label: 'Markdown' },
    { key: 'csv', label: 'CSV' },
  ], []);

  function handleSelect(key: string) {
    if (key === 'json') downloadFile(exportAuditJson(report), `${base}.json`, 'application/json');
    else if (key === 'md') downloadFile(exportAuditMarkdown(report, sessionTitle), `${base}.md`, 'text/markdown');
    else downloadFile(exportAuditCsv(report), `${base}.csv`, 'text/csv');
  }

  return (
    <Dropdown
      trigger={
        <button className="px-3 py-1.5 text-sm border border-[var(--border)] text-[var(--text-secondary)] rounded hover:bg-[var(--bg-tertiary)] transition-colors inline-flex items-center gap-1">
          Export
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </button>
      }
      items={items}
      onSelect={handleSelect}
      menuLabel="Export format"
    />
  );
}
