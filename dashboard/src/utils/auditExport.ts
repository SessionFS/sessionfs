import type { AuditReport } from '../api/client';

export function exportAuditJson(report: AuditReport): string {
  return JSON.stringify(report, null, 2);
}

export function exportAuditMarkdown(report: AuditReport, sessionTitle?: string): string {
  const pct = Math.round(report.summary.trust_score * 100);
  const lines: string[] = [];

  lines.push(`# Audit Report${sessionTitle ? `: ${sessionTitle}` : ''}`);
  lines.push('');
  lines.push(`**Trust Score:** ${pct}%`);
  lines.push(`**Model:** ${report.model}`);
  lines.push(`**Date:** ${new Date(report.timestamp).toLocaleString()}`);
  lines.push(`**Session:** ${report.session_id}`);
  lines.push('');
  lines.push('## Summary');
  lines.push('');
  lines.push(`| Metric | Count |`);
  lines.push(`|--------|-------|`);
  lines.push(`| Total claims | ${report.summary.total_claims} |`);
  lines.push(`| Verified | ${report.summary.verified} |`);
  lines.push(`| Unverified | ${report.summary.unverified} |`);
  lines.push(`| Hallucinations | ${report.summary.hallucinations} |`);
  lines.push('');

  if (report.findings.length > 0) {
    lines.push('## Findings');
    lines.push('');
    for (const f of report.findings) {
      const icon = f.verdict === 'verified' ? 'VERIFIED' : f.verdict === 'unverified' ? 'UNVERIFIED' : 'HALLUCINATION';
      lines.push(`### [${icon}] ${f.claim}`);
      lines.push('');
      lines.push(`- **Severity:** ${f.severity}`);
      lines.push(`- **Message:** #${f.message_index}`);
      lines.push(`- **Evidence:** ${f.evidence}`);
      lines.push(`- **Explanation:** ${f.explanation}`);
      lines.push('');
    }
  }

  return lines.join('\n');
}

export function exportAuditCsv(report: AuditReport): string {
  const rows: string[] = [];
  rows.push('message_index,verdict,severity,claim,evidence,explanation');
  for (const f of report.findings) {
    const escape = (s: string) => `"${s.replace(/"/g, '""')}"`;
    rows.push(
      [f.message_index, f.verdict, f.severity, escape(f.claim), escape(f.evidence), escape(f.explanation)].join(','),
    );
  }
  return rows.join('\n');
}

export function downloadFile(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
