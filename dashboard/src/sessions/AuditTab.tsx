import { useState, useRef, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
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

interface Props {
  sessionId: string;
  messageCount: number;
  sessionTitle?: string;
  onJumpToMessage?: (messageIndex: number) => void;
}

export default function AuditTab({ sessionId, messageCount, sessionTitle, onJumpToMessage }: Props) {
  const { data: report, isLoading, error } = useAudit(sessionId);
  const [showModal, setShowModal] = useState(false);

  const is404 = error && 'status' in error && (error as { status: number }).status === 404;

  if (!isLoading && (is404 || (!report && !error))) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <p className="text-text-secondary mb-2">No audit has been run for this session.</p>
        <p className="text-text-muted text-sm mb-6">
          Auditing evaluates factual claims in the conversation against tool output evidence.
        </p>
        <button
          onClick={() => setShowModal(true)}
          className="px-4 py-2 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors"
        >
          Run Audit
        </button>
        {showModal && (
          <AuditModal sessionTitle={sessionTitle} sessionId={sessionId} messageCount={messageCount}
            onClose={() => setShowModal(false)} onComplete={() => setShowModal(false)} />
        )}
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin mb-3" />
        <p className="text-text-muted text-sm">Loading audit report...</p>
      </div>
    );
  }

  if (error && !is404) {
    return <div className="p-4 text-red-400 text-sm">Failed to load audit: {String(error)}</div>;
  }

  if (!report) return null;

  const contradictions = report.findings.filter(f => f.verdict === 'hallucination');
  const unverified = report.findings.filter(f => f.verdict === 'unverified');
  const verified = report.findings.filter(f => f.verdict === 'verified');
  const pct = Math.round(report.summary.trust_score * 100);

  return (
    <div className="p-4 max-w-3xl mx-auto">
      {/* Metric Cards */}
      <div className="grid grid-cols-4 gap-3 mb-6">
        <MetricCard
          label="Trust Score"
          value={`${pct}%`}
          color={pct >= 85 ? 'text-green-400' : pct >= 60 ? 'text-yellow-400' : 'text-red-400'}
        />
        <MetricCard label="Claims" value={String(report.summary.total_claims)} color="text-text-secondary" />
        <MetricCard label="Contradictions" value={String(contradictions.length)} color={contradictions.length > 0 ? 'text-red-400' : 'text-green-400'} />
        <MetricCard label="Unverified" value={String(unverified.length)} color="text-text-muted" />
      </div>

      {/* Filter badges + actions */}
      <div className="flex items-center gap-2 mb-6 flex-wrap">
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
        <button onClick={() => setShowModal(true)}
          className="px-3 py-1.5 text-sm border border-border text-text-secondary rounded hover:bg-bg-tertiary transition-colors">
          Re-audit
        </button>
        <DownloadDropdown report={report} sessionTitle={sessionTitle} />
      </div>

      {/* Contradictions — always expanded */}
      {contradictions.length > 0 && (
        <section className="mb-6">
          <h3 className="text-sm font-medium text-red-400 mb-1">Contradictions</h3>
          <p className="text-xs text-text-muted mb-3">Claims that directly contradict tool outputs</p>
          <div className="flex flex-col gap-2">
            {contradictions.map((f, i) => (
              <ContradictionCard key={i} finding={f} onJump={onJumpToMessage} />
            ))}
          </div>
        </section>
      )}

      {contradictions.length === 0 && report.summary.total_claims > 0 && (
        <div className="mb-6 p-3 bg-green-500/10 border border-green-500/20 rounded-lg text-sm text-green-400">
          No contradictions found across {report.summary.total_claims} claims.
        </div>
      )}

      {/* Unverified — collapsed */}
      {unverified.length > 0 && (
        <CollapsibleSection title={`${unverified.length} unverified claims`} defaultOpen={false}>
          <div className="flex flex-col gap-1.5">
            {unverified.map((f, i) => (
              <SimpleClaimRow key={i} finding={f} onJump={onJumpToMessage} />
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* Verified — collapsed */}
      {verified.length > 0 && (
        <CollapsibleSection title={`${verified.length} verified claims`} defaultOpen={false}>
          <div className="flex flex-col gap-1">
            {verified.map((f, i) => (
              <div key={i} className="flex items-center gap-2 text-sm text-text-muted py-1">
                <span className="text-green-400 shrink-0">&#10003;</span>
                <span className="truncate">{f.claim}</span>
                <span className="text-text-muted/50 shrink-0 ml-auto">#{f.message_index}</span>
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

      {showModal && (
        <AuditModal sessionTitle={sessionTitle} sessionId={sessionId} messageCount={messageCount}
          onClose={() => setShowModal(false)} onComplete={() => setShowModal(false)} />
      )}
    </div>
  );
}


function MetricCard({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="bg-bg-secondary border border-border rounded-lg p-3 text-center">
      <div className={`text-xl font-semibold tabular-nums ${color}`}>{value}</div>
      <div className="text-[10px] text-text-muted uppercase tracking-wider mt-0.5">{label}</div>
    </div>
  );
}


const SEVERITY_STYLES: Record<string, string> = {
  critical: 'bg-red-500/20 text-red-400',
  high: 'bg-orange-500/20 text-orange-400',
  low: 'bg-bg-tertiary text-text-muted',
  // backward compat
  major: 'bg-red-500/20 text-red-400',
  moderate: 'bg-orange-500/20 text-orange-400',
  minor: 'bg-bg-tertiary text-text-muted',
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


function ContradictionCard({ finding, onJump }: { finding: AuditFinding; onJump?: (idx: number) => void }) {
  return (
    <div className="border border-red-500/30 rounded-lg overflow-hidden bg-red-500/5">
      <div className="px-3 py-2 flex items-center gap-2 flex-wrap">
        <span className={`px-1.5 py-0.5 text-[10px] rounded uppercase font-medium ${SEVERITY_STYLES[finding.severity] || SEVERITY_STYLES.low}`}>
          {finding.severity}
        </span>
        <span className="px-1.5 py-0.5 text-[10px] rounded bg-bg-tertiary text-text-muted">
          {CATEGORY_LABELS[finding.category || 'other'] || finding.category}
        </span>
        <span className="text-text-muted text-xs ml-auto">Message #{finding.message_index}</span>
      </div>
      <div className="px-3 pb-3 space-y-2">
        <div>
          <span className="text-[10px] text-text-muted uppercase block mb-0.5">AI claimed</span>
          <p className="text-sm text-text-primary">{finding.claim}</p>
        </div>
        {finding.evidence && (
          <div>
            <span className="text-[10px] text-text-muted uppercase block mb-0.5">Evidence</span>
            <pre className="bg-bg-tertiary border border-border rounded px-2 py-1.5 text-xs text-text-secondary overflow-x-auto whitespace-pre-wrap max-h-40 overflow-y-auto">
              {finding.evidence}
            </pre>
          </div>
        )}
        <p className="text-xs text-text-muted">{finding.explanation}</p>
        {onJump && (
          <button onClick={() => onJump(finding.message_index)}
            className="text-accent text-xs hover:underline">
            Jump to message #{finding.message_index}
          </button>
        )}
      </div>
    </div>
  );
}


function SimpleClaimRow({ finding, onJump }: { finding: AuditFinding; onJump?: (idx: number) => void }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="border border-border rounded">
      <button onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-2 py-1.5 text-left hover:bg-bg-tertiary text-sm">
        <span className="text-yellow-400 shrink-0">&#9888;</span>
        <span className="text-text-secondary truncate flex-1">{finding.claim}</span>
        <span className="text-text-muted/50 text-xs shrink-0">#{finding.message_index}</span>
      </button>
      {expanded && (
        <div className="px-2 py-2 border-t border-border text-xs text-text-muted space-y-1">
          <p>{finding.explanation || 'No corresponding tool output found to verify this claim.'}</p>
          {onJump && (
            <button onClick={() => onJump(finding.message_index)} className="text-accent hover:underline">
              Jump to message
            </button>
          )}
        </div>
      )}
    </div>
  );
}


function CollapsibleSection({ title, defaultOpen, children }: { title: string; defaultOpen: boolean; children: React.ReactNode }) {
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
      </button>
      {open && children}
    </section>
  );
}


function AuditHistory({ sessionId }: { sessionId: string }) {
  const { auth } = useAuth();
  const { data } = useQuery({
    queryKey: ['auditHistory', sessionId],
    queryFn: async () => {
      const resp = await fetch(`${auth!.baseUrl}/api/v1/sessions/${sessionId}/audits`, {
        headers: { Authorization: `Bearer ${auth!.apiKey}` },
      });
      if (!resp.ok) return [];
      return resp.json();
    },
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
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  const base = `audit-${report.session_id.slice(0, 12)}`;

  function handleDownload(format: 'json' | 'md' | 'csv') {
    setOpen(false);
    if (format === 'json') downloadFile(exportAuditJson(report), `${base}.json`, 'application/json');
    else if (format === 'md') downloadFile(exportAuditMarkdown(report, sessionTitle), `${base}.md`, 'text/markdown');
    else downloadFile(exportAuditCsv(report), `${base}.csv`, 'text/csv');
  }

  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(!open)}
        className="px-3 py-1.5 text-sm border border-border text-text-secondary rounded hover:bg-bg-tertiary transition-colors inline-flex items-center gap-1">
        Export
        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 bg-bg-secondary border border-border rounded-lg shadow-lg z-10 min-w-[120px]">
          <button onClick={() => handleDownload('json')} className="w-full text-left px-3 py-2 text-sm text-text-secondary hover:bg-bg-tertiary rounded-t-lg">JSON</button>
          <button onClick={() => handleDownload('md')} className="w-full text-left px-3 py-2 text-sm text-text-secondary hover:bg-bg-tertiary">Markdown</button>
          <button onClick={() => handleDownload('csv')} className="w-full text-left px-3 py-2 text-sm text-text-secondary hover:bg-bg-tertiary rounded-b-lg">CSV</button>
        </div>
      )}
    </div>
  );
}
