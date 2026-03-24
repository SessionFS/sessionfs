import { useState, useRef, useEffect } from 'react';
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
}

export default function AuditTab({ sessionId, messageCount, sessionTitle }: Props) {
  const { data: report, isLoading, error } = useAudit(sessionId);
  const [showModal, setShowModal] = useState(false);

  const is404 = error && 'status' in error && (error as { status: number }).status === 404;

  // No audit exists yet
  if (!isLoading && (is404 || (!report && !error))) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <p className="text-text-secondary mb-2">No audit has been run for this session.</p>
        <p className="text-text-muted text-sm mb-6">
          Auditing evaluates factual claims in the conversation against evidence.
        </p>
        <button
          onClick={() => setShowModal(true)}
          className="px-4 py-2 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors"
        >
          Run Audit
        </button>
        {showModal && (
          <AuditModal
            sessionId={sessionId}
            messageCount={messageCount}
            onClose={() => setShowModal(false)}
            onComplete={() => setShowModal(false)}
          />
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
    return (
      <div className="p-4 text-red-400 text-sm">
        Failed to load audit: {String(error)}
      </div>
    );
  }

  if (!report) return null;

  return (
    <div className="p-4 max-w-3xl mx-auto">
      {/* Trust Score Bar */}
      <TrustScoreBar score={report.summary.trust_score} />

      {/* Summary */}
      <div className="mb-6 text-sm text-text-secondary">
        {report.summary.total_claims} claims evaluated:{' '}
        <span className="text-green-400">{report.summary.verified} verified</span>
        {report.summary.unverified > 0 && (
          <>, <span className="text-yellow-400">{report.summary.unverified} unverified</span></>
        )}
        {report.summary.hallucinations > 0 && (
          <>, <span className="text-red-400">{report.summary.hallucinations} hallucinations</span></>
        )}
      </div>

      {/* Meta */}
      <div className="flex gap-4 text-xs text-text-muted mb-6">
        <span>Model: {report.model}</span>
        <span>Run: {new Date(report.timestamp).toLocaleString()}</span>
      </div>

      {/* Re-run + Download */}
      <div className="flex items-center gap-2 mb-6">
        <button
          onClick={() => setShowModal(true)}
          className="px-3 py-1.5 text-xs border border-border text-text-secondary rounded hover:bg-bg-tertiary transition-colors"
        >
          Re-run Audit
        </button>
        <DownloadDropdown report={report} sessionTitle={sessionTitle} />
      </div>

      {/* Findings */}
      <div className="flex flex-col gap-2">
        {report.findings.map((f, i) => (
          <FindingRow key={i} finding={f} />
        ))}
      </div>

      {report.findings.length === 0 && (
        <p className="text-text-muted text-sm py-4">No findings to display.</p>
      )}

      {showModal && (
        <AuditModal
          sessionId={sessionId}
          messageCount={messageCount}
          onClose={() => setShowModal(false)}
          onComplete={() => setShowModal(false)}
        />
      )}
    </div>
  );
}

function TrustScoreBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color =
    pct >= 90 ? 'bg-green-500' : pct >= 70 ? 'bg-yellow-500' : 'bg-red-500';
  const textColor =
    pct >= 90 ? 'text-green-400' : pct >= 70 ? 'text-yellow-400' : 'text-red-400';

  return (
    <div className="mb-6">
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-xs text-text-muted uppercase tracking-wider">Trust Score</span>
        <span className={`text-2xl font-semibold tabular-nums ${textColor}`}>{pct}%</span>
      </div>
      <div className="h-2 bg-bg-tertiary rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function FindingRow({ finding }: { finding: AuditFinding }) {
  const [expanded, setExpanded] = useState(false);

  const verdictIcon =
    finding.verdict === 'verified'
      ? '\u2705'
      : finding.verdict === 'unverified'
        ? '\u26A0\uFE0F'
        : '\uD83D\uDD34';

  const severityClasses: Record<string, string> = {
    minor: 'bg-bg-tertiary text-text-muted',
    moderate: 'bg-yellow-500/20 text-yellow-400',
    major: 'bg-red-500/20 text-red-400',
  };

  const looksLikeCode =
    finding.evidence.includes('\n') ||
    finding.evidence.startsWith('$') ||
    finding.evidence.startsWith('>');

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-bg-tertiary transition-colors"
      >
        <span className="text-sm shrink-0">{verdictIcon}</span>
        <span className={`px-1.5 py-0.5 text-[10px] rounded shrink-0 ${severityClasses[finding.severity]}`}>
          {finding.severity}
        </span>
        <span className="text-sm text-text-primary truncate flex-1">{finding.claim}</span>
        <span className="text-text-muted text-xs shrink-0 ml-2">#{finding.message_index}</span>
        <svg
          className={`w-3 h-3 text-text-muted shrink-0 transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {expanded && (
        <div className="px-3 py-3 border-t border-border bg-bg-primary text-sm space-y-3">
          <div>
            <span className="text-xs text-text-muted block mb-1">Evidence</span>
            {looksLikeCode ? (
              <pre className="bg-bg-tertiary border border-border rounded px-3 py-2 text-xs text-text-secondary overflow-x-auto whitespace-pre-wrap">
                {finding.evidence}
              </pre>
            ) : (
              <p className="text-text-secondary text-sm">{finding.evidence}</p>
            )}
          </div>
          <div>
            <span className="text-xs text-text-muted block mb-1">Explanation</span>
            <p className="text-text-secondary text-sm">{finding.explanation}</p>
          </div>
          <a
            href={`#message-${finding.message_index}`}
            className="text-accent text-xs hover:underline"
          >
            Jump to message #{finding.message_index}
          </a>
        </div>
      )}
    </div>
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
    if (format === 'json') {
      downloadFile(exportAuditJson(report), `${base}.json`, 'application/json');
    } else if (format === 'md') {
      downloadFile(exportAuditMarkdown(report, sessionTitle), `${base}.md`, 'text/markdown');
    } else {
      downloadFile(exportAuditCsv(report), `${base}.csv`, 'text/csv');
    }
  }

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="px-3 py-1.5 text-xs border border-border text-text-secondary rounded hover:bg-bg-tertiary transition-colors inline-flex items-center gap-1"
      >
        Download
        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 bg-bg-secondary border border-border rounded-lg shadow-lg z-10 min-w-[120px]">
          <button
            onClick={() => handleDownload('json')}
            className="w-full text-left px-3 py-2 text-xs text-text-secondary hover:bg-bg-tertiary transition-colors rounded-t-lg"
          >
            JSON
          </button>
          <button
            onClick={() => handleDownload('md')}
            className="w-full text-left px-3 py-2 text-xs text-text-secondary hover:bg-bg-tertiary transition-colors"
          >
            Markdown
          </button>
          <button
            onClick={() => handleDownload('csv')}
            className="w-full text-left px-3 py-2 text-xs text-text-secondary hover:bg-bg-tertiary transition-colors rounded-b-lg"
          >
            CSV
          </button>
        </div>
      )}
    </div>
  );
}
