/**
 * v0.10.3 — AgentRuns tab (v0.10.2 backend, v0.10.3 dashboard surface).
 *
 * Read-only audit trail. AgentRuns are CI-driven — `sfs agent run` /
 * `sfs agent complete` create and terminalize them from inside a CI
 * runner. The dashboard surfaces them for inspection: who ran, what
 * triggered it, the worst severity, the policy verdict, and the
 * structured findings JSON.
 */

import { useState } from 'react';
import { useAgentRuns } from '../hooks/useAgentRuns';
import RelativeDate from '../components/RelativeDate';
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

const STATUS_TONE: Record<string, string> = {
  queued: 'bg-muted/15 text-muted',
  running: 'bg-blue-500/15 text-blue-600',
  passed: 'bg-emerald-500/15 text-emerald-600',
  failed: 'bg-danger/15 text-danger',
  errored: 'bg-amber-500/15 text-amber-600',
  cancelled: 'bg-muted/15 text-muted',
};

const SEVERITY_TONE: Record<string, string> = {
  none: 'text-muted',
  low: 'text-muted',
  medium: 'text-amber-600',
  high: 'text-danger',
  critical: 'text-danger font-semibold',
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${
        STATUS_TONE[status] ?? 'bg-muted/15 text-muted'
      }`}
    >
      {status}
    </span>
  );
}

function PolicyBadge({ result, exitCode }: { result: string | null; exitCode: number | null }) {
  if (!result) return <span className="text-muted">—</span>;
  const tone =
    result === 'fail'
      ? 'bg-danger/15 text-danger'
      : 'bg-emerald-500/15 text-emerald-600';
  return (
    <span className="flex items-center gap-1.5">
      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${tone}`}>
        {result}
      </span>
      {exitCode !== null && (
        <span className="font-mono text-xs text-muted">exit {exitCode}</span>
      )}
    </span>
  );
}

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

  if (isLoading) return <p>Loading runs…</p>;
  if (error) return <p role="alert">Failed to load runs: {String(error)}</p>;
  if (!data) return null;

  return (
    <section aria-labelledby="agent-runs-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h2 id="agent-runs-heading" className="text-lg font-semibold">
          Agent runs
          <span className="ml-2 text-sm text-muted">
            {data.length} {data.length === 1 ? 'run' : 'runs'}
          </span>
        </h2>
        <div className="flex items-center gap-2 flex-wrap">
          <label className="text-sm">
            <span className="sr-only">Filter by status</span>
            <select
              aria-label="Filter by status"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="px-2 py-1 border border-border rounded text-sm bg-surface"
            >
              {STATUS_FILTERS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="sr-only">Filter by trigger</span>
            <select
              aria-label="Filter by trigger"
              value={triggerFilter}
              onChange={(e) => setTriggerFilter(e.target.value)}
              className="px-2 py-1 border border-border rounded text-sm bg-surface"
            >
              {TRIGGER_FILTERS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <input
            type="text"
            aria-label="Filter by persona"
            placeholder="persona"
            value={personaFilter}
            onChange={(e) => setPersonaFilter(e.target.value)}
            className="px-2 py-1 border border-border rounded text-sm bg-surface font-mono w-32"
          />
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded border border-border hover:bg-surface"
            onClick={() => void refetch()}
            disabled={isFetching}
          >
            {isFetching ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      <p className="text-xs text-muted">
        Runs are recorded by <code className="font-mono">sfs agent run</code> in CI.
        This tab is read-only; refreshes every 30 seconds while open.
      </p>

      {data.length === 0 ? (
        <div className="border border-border rounded p-6 text-center text-muted">
          No runs yet. Wire up <code className="font-mono">sfs agent run</code> in
          your CI workflow to start recording.
        </div>
      ) : (
        <ul className="border border-border rounded divide-y divide-border">
          {data.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                onClick={() => setExpanded(expanded === r.id ? null : r.id)}
                aria-expanded={expanded === r.id}
                className="w-full text-left px-3 py-2 hover:bg-surface"
              >
                <div className="flex items-center gap-3 text-sm">
                  <span className="font-mono text-xs text-muted">{r.id}</span>
                  <StatusBadge status={r.status} />
                  <span className="font-mono">{r.persona_name}</span>
                  <span className="text-xs text-muted">via {r.trigger_source}</span>
                  {r.severity && (
                    <span className={`text-xs ${SEVERITY_TONE[r.severity] ?? ''}`}>
                      severity: {r.severity}
                    </span>
                  )}
                  <span className="text-xs text-muted">
                    {r.findings_count} {r.findings_count === 1 ? 'finding' : 'findings'}
                  </span>
                  <PolicyBadge result={r.policy_result} exitCode={r.exit_code} />
                  <span className="ml-auto text-xs text-muted">
                    <RelativeDate iso={r.created_at} />
                  </span>
                </div>
                {r.result_summary && (
                  <div className="text-sm text-muted mt-1 truncate">{r.result_summary}</div>
                )}
              </button>
              {expanded === r.id && <RunDetail run={r} />}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function RunDetail({ run }: { run: AgentRun }) {
  return (
    <div className="px-3 py-3 bg-surface/50 text-sm space-y-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <div>
          <dt className="text-muted">Tool</dt>
          <dd className="font-mono">{run.tool}</dd>
        </div>
        <div>
          <dt className="text-muted">Ticket</dt>
          <dd className="font-mono">{run.ticket_id ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">Trigger ref</dt>
          <dd className="font-mono truncate">{run.trigger_ref ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">CI provider</dt>
          <dd>
            {(() => {
              const href = safeHttpUrl(run.ci_run_url);
              if (href) {
                return (
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-brand hover:underline"
                  >
                    {run.ci_provider ?? 'open'}
                  </a>
                );
              }
              return run.ci_provider ?? '—';
            })()}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Fail-on</dt>
          <dd className="font-mono">{run.fail_on ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">Duration</dt>
          <dd className="font-mono">
            {run.duration_seconds !== null ? `${run.duration_seconds}s` : '—'}
          </dd>
        </div>
      </dl>

      {run.result_summary && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-muted mb-1">Summary</h4>
          <p className="whitespace-pre-wrap">{run.result_summary}</p>
        </div>
      )}

      {run.findings.length > 0 && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-muted mb-1">
            Findings ({run.findings.length})
          </h4>
          <pre className="text-xs bg-bg border border-border rounded p-2 overflow-x-auto">
            {JSON.stringify(run.findings, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
