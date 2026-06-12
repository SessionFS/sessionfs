/**
 * v0.10.3 — AgentRuns tab (v0.10.2 backend, v0.10.3 dashboard surface).
 *
 * Read-only audit trail. AgentRuns are CI-driven — `sfs agent run` /
 * `sfs agent complete` create and terminalize them from inside a CI
 * runner. The dashboard surfaces them for inspection: who ran, what
 * triggered it, the worst severity, the policy verdict, and the
 * structured findings JSON.
 *
 * Phase 3 restyle: status chips → Badge variants, filters → Select,
 * actions → Button, IDs → mono-chip, detail panel → sunken ladder.
 */

import { useState } from 'react';
import { useAgentRuns } from '../hooks/useAgentRuns';
import RelativeDate from '../components/RelativeDate';
import { Badge } from '../components/Badge';
import { Select, Button, Card } from '../components/ui';
import type { AgentRun } from '../api/client';

/**
 * AgentRun `ci_run_url` is operator-supplied (`--ci-run-url` flag on
 * `sfs agent run`) and stored verbatim. Treat it as untrusted — only
 * render it as a clickable link when the scheme is http(s). Anything
 * else (`javascript:`, `data:`, malformed) falls back to plain text so
 * a crafted run row cannot execute code on click. Codex R1 HIGH fix.
 */
function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    if (u.protocol === 'http:' || u.protocol === 'https:') {
      return u.toString();
    }
  } catch {
    // Falls through to null — malformed URLs are not clickable.
  }
  return null;
}

interface AgentRunsTabProps {
  projectId: string;
}

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'queued', label: 'Queued' },
  { value: 'running', label: 'Running' },
  { value: 'passed', label: 'Passed' },
  { value: 'failed', label: 'Failed' },
  { value: 'errored', label: 'Errored' },
  { value: 'cancelled', label: 'Cancelled' },
];

const TRIGGER_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All triggers' },
  { value: 'ci', label: 'CI' },
  { value: 'webhook', label: 'Webhook' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'manual', label: 'Manual' },
  { value: 'mcp', label: 'MCP' },
  { value: 'api', label: 'API' },
];

/** Map AgentRun statuses to Badge variants. */
const STATUS_VARIANT: Record<string, 'default' | 'success' | 'warning' | 'danger' | 'info'> = {
  queued: 'default',
  running: 'info',
  passed: 'success',
  failed: 'danger',
  errored: 'warning',
  cancelled: 'default',
};

const SEVERITY_TONE: Record<string, string> = {
  none: 'text-[var(--text-tertiary)]',
  low: 'text-[var(--text-tertiary)]',
  medium: 'text-[var(--warning)]',
  high: 'text-[var(--danger)]',
  critical: 'text-[var(--danger)] font-semibold',
};

export default function AgentRunsTab({ projectId }: AgentRunsTabProps) {
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [triggerFilter, setTriggerFilter] = useState<string>('');
  const [personaFilter, setPersonaFilter] = useState<string>('');
  const [expanded, setExpanded] = useState<string | null>(null);

  const { data, isLoading, error, refetch, isFetching } = useAgentRuns(projectId, {
    status: statusFilter || undefined,
    trigger_source: triggerFilter || undefined,
    persona_name: personaFilter.trim() || undefined,
    limit: 50,
  });

  if (isLoading) return <p className="text-[var(--text-tertiary)] p-4">Loading runs…</p>;
  if (error) return <p role="alert" className="text-[var(--danger)] p-4">Failed to load runs: {String(error)}</p>;
  if (!data) return null;

  const runs = data;

  return (
    <section aria-labelledby="agent-runs-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h2 id="agent-runs-heading" className="flex items-baseline gap-2">
          <span className="text-lg font-semibold text-[var(--text-primary)]">Agent runs</span>
          <span className="text-sm text-[var(--text-tertiary)]">
            {runs.length} {runs.length === 1 ? 'run' : 'runs'}
          </span>
        </h2>
        <div className="flex items-center gap-2 flex-wrap">
          <Select
            aria-label="Filter by status"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            options={STATUS_FILTERS}
            className="w-auto min-w-[110px]"
          />
          <Select
            aria-label="Filter by trigger"
            value={triggerFilter}
            onChange={(e) => setTriggerFilter(e.target.value)}
            options={TRIGGER_FILTERS}
            className="w-auto min-w-[130px]"
          />
          <input
            type="text"
            aria-label="Filter by persona"
            placeholder="persona"
            value={personaFilter}
            onChange={(e) => setPersonaFilter(e.target.value)}
            className="w-28 bg-[var(--bg-sunken)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] font-mono placeholder:text-[var(--text-tertiary)] outline-none focus:border-[var(--brand)] focus:shadow-[0_0_0_3px_var(--brand-glow)]"
          />
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void refetch()}
            disabled={isFetching}
          >
            {isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
      </div>

      <p className="text-[11px] uppercase tracking-[0.04em] text-[var(--text-tertiary)]">
        Runs are recorded by <code className="font-mono text-[var(--text-secondary)]">sfs agent run</code> in CI.
        This tab is read-only; refreshes every 30 seconds while open.
      </p>

      {runs.length === 0 ? (
        <div className="rounded-lg border border-[var(--border)] p-8 text-center" style={{ backgroundColor: 'var(--surface)' }}>
          <svg className="mx-auto mb-3 opacity-30" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
          </svg>
          <p className="text-[15px] font-semibold text-[var(--text-primary)] mb-1">No runs yet</p>
          <p className="text-[13px] text-[var(--text-tertiary)]">
            Wire up <code className="font-mono text-[var(--text-secondary)]">sfs agent run</code> in
            your CI workflow to start recording.
          </p>
        </div>
      ) : (
        <Card className="divide-y divide-[var(--border)] p-0">
          {runs.map((r) => (
            <div key={r.id}>
              <button
                type="button"
                onClick={() => setExpanded(expanded === r.id ? null : r.id)}
                aria-expanded={expanded === r.id}
                className="w-full text-left px-4 py-2.5 hover:bg-[var(--surface-hover)] transition-colors duration-150"
              >
                <div className="flex items-center gap-3 text-sm flex-wrap">
                  <code className="text-mono-chip">{r.id}</code>
                  <Badge
                    variant={STATUS_VARIANT[r.status] ?? 'default'}
                    tint
                    label={r.status}
                    size="sm"
                  />
                  <span className="font-mono text-[13px] text-[var(--text-secondary)]">{r.persona_name}</span>
                  <span className="text-xs text-[var(--text-tertiary)]">via {r.trigger_source}</span>
                  {r.severity && (
                    <span className={`text-xs ${SEVERITY_TONE[r.severity] ?? ''}`}>
                      severity: {r.severity}
                    </span>
                  )}
                  <span className="text-xs text-[var(--text-tertiary)]">
                    {r.findings_count} {r.findings_count === 1 ? 'finding' : 'findings'}
                  </span>
                  {r.policy_result && (
                    <span className="flex items-center gap-1.5">
                      <Badge
                        variant={r.policy_result === 'fail' ? 'danger' : 'success'}
                        label={r.policy_result}
                        size="sm"
                      />
                      {r.exit_code !== null && (
                        <span className="font-mono text-xs text-[var(--text-tertiary)]">exit {r.exit_code}</span>
                      )}
                    </span>
                  )}
                  {!r.policy_result && (
                    <span className="text-[var(--text-tertiary)] text-xs">—</span>
                  )}
                  <span className="ml-auto text-xs text-[var(--text-tertiary)]">
                    <RelativeDate iso={r.created_at} />
                  </span>
                </div>
                {r.result_summary && (
                  <div className="text-sm text-[var(--text-tertiary)] mt-1 truncate">{r.result_summary}</div>
                )}
              </button>
              {expanded === r.id && <RunDetail run={r} />}
            </div>
          ))}
        </Card>
      )}
    </section>
  );
}

function RunDetail({ run }: { run: AgentRun }) {
  return (
    <div className="px-4 py-3 text-sm space-y-3" style={{ backgroundColor: 'var(--bg-sunken)' }}>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <div>
          <dt className="text-[11px] uppercase tracking-[0.04em] text-[var(--text-tertiary)]">Tool</dt>
          <dd className="font-mono text-[var(--text-primary)] mt-0.5">{run.tool}</dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-[0.04em] text-[var(--text-tertiary)]">Ticket</dt>
          <dd className="font-mono text-[var(--text-primary)] mt-0.5">{run.ticket_id ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-[0.04em] text-[var(--text-tertiary)]">Trigger ref</dt>
          <dd className="font-mono text-[var(--text-primary)] mt-0.5 truncate">{run.trigger_ref ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-[0.04em] text-[var(--text-tertiary)]">CI provider</dt>
          <dd className="mt-0.5">
            {(() => {
              const href = safeHttpUrl(run.ci_run_url);
              if (href) {
                return (
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--brand)] hover:underline"
                  >
                    {run.ci_provider ?? 'open'}
                  </a>
                );
              }
              return <span className="text-[var(--text-primary)]">{run.ci_provider ?? '—'}</span>;
            })()}
          </dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-[0.04em] text-[var(--text-tertiary)]">Fail-on</dt>
          <dd className="font-mono text-[var(--text-primary)] mt-0.5">{run.fail_on ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-[0.04em] text-[var(--text-tertiary)]">Duration</dt>
          <dd className="font-mono text-[var(--text-primary)] mt-0.5">
            {run.duration_seconds !== null ? `${run.duration_seconds}s` : '—'}
          </dd>
        </div>
      </dl>

      {run.result_summary && (
        <div>
          <h4 className="text-micro text-[var(--text-tertiary)] mb-1">Summary</h4>
          <p className="text-[var(--text-secondary)] whitespace-pre-wrap">{run.result_summary}</p>
        </div>
      )}

      {run.findings.length > 0 && (
        <div>
          <h4 className="text-micro text-[var(--text-tertiary)] mb-1">
            Findings ({run.findings.length})
          </h4>
          <pre
            className="text-xs p-2 rounded-lg border border-[var(--border)] overflow-x-auto"
            style={{ backgroundColor: 'var(--bg-primary)' }}
          >
            {JSON.stringify(run.findings, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
