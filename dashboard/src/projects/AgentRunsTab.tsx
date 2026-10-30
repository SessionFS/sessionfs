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
 *
 * GitHub #95: each run can be replayed with an edited prompt/ticket/
 * context from a dialog (which surfaces the server-generated command +
 * CI snippet), child runs link back to their parent, and two runs can
 * be compared for a findings diff.
 */

import { useEffect, useState } from 'react';
import {
  useAgentRun,
  useAgentRuns,
  useCompareAgentRuns,
  useReplayAgentRun,
} from '../hooks/useAgentRuns';
import RelativeDate from '../components/RelativeDate';
import CopyButton from '../components/CopyButton';
import { Badge } from '../components/Badge';
import {
  Select,
  Button,
  Card,
  Input,
  Textarea,
  Dialog,
  DialogHeader,
  DialogFooter,
} from '../components/ui';
import { ApiError, type AgentRun, type AgentRunReplayInput, type AgentRunReplayResult } from '../api/client';
import { errorMessage } from '../utils/errorMessage';
import { safeHttpUrl } from '../utils/url';

// AgentRun `ci_run_url` is operator-supplied and stored verbatim, so it is
// treated as untrusted: safeHttpUrl only links http(s) URLs and falls back to
// plain text for anything else.

interface AgentRunsTabProps {
  projectId: string;
  /** Run to open on arrival (URL `run=`). */
  initialRunId?: string | null;
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

const FAIL_ON_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Inherit from parent' },
  { value: 'none', label: 'none' },
  { value: 'low', label: 'low' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high' },
  { value: 'critical', label: 'critical' },
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
  none: 'text-text-tertiary',
  low: 'text-text-tertiary',
  medium: 'text-warning',
  high: 'text-danger',
  critical: 'text-danger font-semibold',
};

export default function AgentRunsTab({ projectId, initialRunId = null }: AgentRunsTabProps) {
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [triggerFilter, setTriggerFilter] = useState<string>('');
  const [personaFilter, setPersonaFilter] = useState<string>('');
  const [expanded, setExpanded] = useState<string | null>(initialRunId);
  // A different ?run= while mounted opens that run.
  const [seenRunId, setSeenRunId] = useState(initialRunId);
  if (seenRunId !== initialRunId) {
    setSeenRunId(initialRunId);
    if (initialRunId) setExpanded(initialRunId);
  }
  const [replayTarget, setReplayTarget] = useState<AgentRun | null>(null);
  const [compareTarget, setCompareTarget] = useState<AgentRun | null>(null);

  const { data, isLoading, error, refetch, isFetching } = useAgentRuns(projectId, {
    status: statusFilter || undefined,
    trigger_source: triggerFilter || undefined,
    persona_name: personaFilter.trim() || undefined,
    limit: 50,
  });

  // Arriving from a link (?run=<id>): bring the opened run into view once listed.
  const hasInitialRun = !!initialRunId && !!data?.some((r) => r.id === initialRunId);
  useEffect(() => {
    if (hasInitialRun) {
      document.getElementById(`run-${initialRunId}`)?.scrollIntoView({ block: 'nearest' });
    }
  }, [hasInitialRun, initialRunId]);

  function openRun(id: string) {
    setExpanded(id);
    requestAnimationFrame(() => {
      document.getElementById(`run-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  }

  if (isLoading) return <p className="text-text-tertiary p-4">Loading runs…</p>;
  if (error) return <p role="alert" className="text-danger p-4">Failed to load runs: {errorMessage(error)}</p>;
  if (!data) return null;

  const runs = data;

  return (
    <section aria-labelledby="agent-runs-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h2 id="agent-runs-heading" className="flex items-baseline gap-2">
          <span className="text-lg font-semibold text-text-primary">Agent runs</span>
          <span className="text-sm text-text-tertiary">
            {runs.length} {runs.length === 1 ? 'run' : 'runs'}
          </span>
        </h2>
        <div className="flex items-center gap-2 flex-wrap">
          <Select
            aria-label="Filter by status"
            value={statusFilter}
            onValueChange={setStatusFilter}
            options={STATUS_FILTERS}
            className="w-auto min-w-[110px]"
          />
          <Select
            aria-label="Filter by trigger"
            value={triggerFilter}
            onValueChange={setTriggerFilter}
            options={TRIGGER_FILTERS}
            className="w-auto min-w-[130px]"
          />
          <input
            type="text"
            aria-label="Filter by persona"
            placeholder="persona"
            value={personaFilter}
            onChange={(e) => setPersonaFilter(e.target.value)}
            className="w-28 bg-bg-sunken border border-border rounded-lg px-3 py-2 text-sm text-text-primary font-mono placeholder:text-text-tertiary outline-none focus:border-[var(--brand)] focus:shadow-[0_0_0_3px_var(--brand-glow)]"
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

      <p className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">
        Runs are recorded by <code className="font-mono text-text-secondary">sfs agent run</code> in CI.
        This tab is read-only; refreshes every 30 seconds while open.
      </p>

      {initialRunId && !runs.some((r) => r.id === initialRunId) && (
        <LinkedRun
          projectId={projectId}
          runId={initialRunId}
          onReplay={setReplayTarget}
          onCompare={setCompareTarget}
          onOpenRun={openRun}
        />
      )}

      {runs.length === 0 ? (
        <div className="rounded-lg border border-border p-8 text-center bg-surface">
          <svg className="mx-auto mb-3 opacity-30" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
          </svg>
          <p className="text-md font-semibold text-text-primary mb-1">No runs yet</p>
          <p className="text-sm text-text-tertiary">
            Wire up <code className="font-mono text-text-secondary">sfs agent run</code> in
            your CI workflow to start recording.
          </p>
        </div>
      ) : (
        <Card className="divide-y divide-[var(--border)] p-0">
          {runs.map((r) => (
            <div key={r.id} id={`run-${r.id}`}>
              <button
                type="button"
                onClick={() => setExpanded(expanded === r.id ? null : r.id)}
                aria-expanded={expanded === r.id}
                className="w-full text-left px-4 py-2.5 hover:bg-surface-hover transition-colors duration-150 outline-none focus-visible:shadow-[inset_0_0_0_2px_var(--brand-glow)]"
              >
                <div className="flex items-center gap-3 text-sm flex-wrap">
                  <code className="text-mono-chip">{r.id}</code>
                  <Badge
                    variant={STATUS_VARIANT[r.status] ?? 'default'}
                    tint
                    label={r.status}
                    size="sm"
                  />
                  <span className="font-mono text-sm text-text-secondary">{r.persona_name}</span>
                  <span className="text-xs text-text-tertiary">via {r.trigger_source}</span>
                  {r.replayed_from && (
                    <span className="text-xs text-text-tertiary" title={`Replayed from ${r.replayed_from}`}>
                      ↩ <code className="text-mono-chip">{r.replayed_from}</code>
                    </span>
                  )}
                  {r.severity && (
                    <span className={`text-xs ${SEVERITY_TONE[r.severity] ?? ''}`}>
                      severity: {r.severity}
                    </span>
                  )}
                  <span className="text-xs text-text-tertiary">
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
                        <span className="font-mono text-xs text-text-tertiary">exit {r.exit_code}</span>
                      )}
                    </span>
                  )}
                  {!r.policy_result && (
                    <span className="text-text-tertiary text-xs">—</span>
                  )}
                  <span className="ml-auto text-xs text-text-tertiary">
                    <RelativeDate iso={r.created_at} />
                  </span>
                </div>
                {r.result_summary && (
                  <div className="text-sm text-text-tertiary mt-1 truncate">{r.result_summary}</div>
                )}
              </button>
              {expanded === r.id && (
                <RunDetail
                  run={r}
                  onReplay={() => setReplayTarget(r)}
                  onCompare={() => setCompareTarget(r)}
                  onOpenRun={openRun}
                />
              )}
            </div>
          ))}
        </Card>
      )}

      {replayTarget && (
        <ReplayDialog
          projectId={projectId}
          run={replayTarget}
          onClose={() => setReplayTarget(null)}
        />
      )}
      {compareTarget && (
        <CompareDialog
          projectId={projectId}
          baseRun={compareTarget}
          otherRuns={runs.filter((r) => r.id !== compareTarget.id)}
          onClose={() => setCompareTarget(null)}
        />
      )}
    </section>
  );
}

/** A run named by ?run= that is not in the listed page: fetched by id and pinned above the list. */
function LinkedRun({
  projectId,
  runId,
  onReplay,
  onCompare,
  onOpenRun,
}: {
  projectId: string;
  runId: string;
  onReplay: (r: AgentRun) => void;
  onCompare: (r: AgentRun) => void;
  onOpenRun: (id: string) => void;
}) {
  const { data: run, isLoading, error } = useAgentRun(projectId, runId);
  if (isLoading) return <p className="text-sm text-text-tertiary">Loading run…</p>;
  if (error || !run) {
    return (
      <p role="status" className="text-sm text-text-tertiary">
        {error instanceof ApiError && error.status === 404 ? 'Run not found.' : `Could not load run ${runId}.`}
      </p>
    );
  }
  return (
    <Card className="p-0" data-testid="linked-run">
      <div id={`run-${run.id}`} className="px-4 pt-3 text-sm">
        <code className="text-mono-chip">{run.id}</code>
        <span className="ml-2 text-xs text-text-tertiary">Linked run, not in the latest 50</span>
      </div>
      <RunDetail run={run} onReplay={() => onReplay(run)} onCompare={() => onCompare(run)} onOpenRun={onOpenRun} />
    </Card>
  );
}

function RunDetail({
  run,
  onReplay,
  onCompare,
  onOpenRun,
}: {
  run: AgentRun;
  onReplay: () => void;
  onCompare: () => void;
  onOpenRun: (id: string) => void;
}) {
  return (
    <div className="px-4 py-3 text-sm space-y-3 bg-bg-sunken">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <div>
          <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Tool</dt>
          <dd className="font-mono text-text-primary mt-0.5">{run.tool}</dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Ticket</dt>
          <dd className="font-mono text-text-primary mt-0.5">{run.ticket_id ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Trigger ref</dt>
          <dd className="font-mono text-text-primary mt-0.5 truncate">{run.trigger_ref ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">CI provider</dt>
          <dd className="mt-0.5">
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
              return <span className="text-text-primary">{run.ci_provider ?? '—'}</span>;
            })()}
          </dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Fail-on</dt>
          <dd className="font-mono text-text-primary mt-0.5">{run.fail_on ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Duration</dt>
          <dd className="font-mono text-text-primary mt-0.5">
            {run.duration_seconds !== null ? `${run.duration_seconds}s` : '—'}
          </dd>
        </div>
        {run.replayed_from && (
          <div>
            <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Replayed from</dt>
            <dd className="mt-0.5">
              <button
                type="button"
                onClick={() => onOpenRun(run.replayed_from!)}
                className="text-brand hover:underline font-mono"
              >
                {run.replayed_from}
              </button>
            </dd>
          </div>
        )}
      </dl>

      {run.prompt && (
        <div>
          <h3 className="text-micro text-text-tertiary mb-1">Prompt</h3>
          <p className="text-text-secondary whitespace-pre-wrap">{run.prompt}</p>
        </div>
      )}

      {run.context_refs.length > 0 && (
        <div>
          <h3 className="text-micro text-text-tertiary mb-1">Context refs</h3>
          <ul className="list-disc list-inside text-text-secondary">
            {run.context_refs.map((ref, i) => (
              <li key={`${ref}-${i}`} className="font-mono text-xs">{ref}</li>
            ))}
          </ul>
        </div>
      )}

      {run.result_summary && (
        <div>
          <h3 className="text-micro text-text-tertiary mb-1">Summary</h3>
          <p className="text-text-secondary whitespace-pre-wrap">{run.result_summary}</p>
        </div>
      )}

      {run.findings.length > 0 && (
        <div>
          <h3 className="text-micro text-text-tertiary mb-1">
            Findings ({run.findings.length})
          </h3>
          <pre
            className="text-xs p-2 rounded-lg border border-border overflow-x-auto bg-bg-primary"
          >
            {JSON.stringify(run.findings, null, 2)}
          </pre>
        </div>
      )}

      <div className="flex items-center gap-2 pt-1">
        <Button variant="secondary" size="sm" onClick={onReplay}>
          Replay
        </Button>
        <Button variant="secondary" size="sm" onClick={onCompare}>
          Compare
        </Button>
      </div>
    </div>
  );
}

function ReplayDialog({
  projectId,
  run,
  onClose,
}: {
  projectId: string;
  run: AgentRun;
  onClose: () => void;
}) {
  const replay = useReplayAgentRun(projectId);
  const [personaName, setPersonaName] = useState('');
  const [ticketId, setTicketId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [contextRefs, setContextRefs] = useState('');
  const [failOn, setFailOn] = useState('');
  const [triggerRef, setTriggerRef] = useState('');
  const [result, setResult] = useState<AgentRunReplayResult | null>(null);

  function submit() {
    const body: AgentRunReplayInput = {};
    if (personaName.trim()) body.persona_name = personaName.trim();
    if (ticketId.trim()) body.ticket_id = ticketId.trim();
    if (prompt) body.prompt = prompt;
    // Match the server's context_refs limits: ≤50 refs, each ≤500 chars,
    // stripped, empties dropped.
    const refs = contextRefs
      .split('\n')
      .map((line) => line.trim().slice(0, 500))
      .filter(Boolean)
      .slice(0, 50);
    if (refs.length) body.context_refs = refs;
    if (failOn) body.fail_on = failOn;
    if (triggerRef.trim()) body.trigger_ref = triggerRef.trim();
    replay.mutate({ runId: run.id, body }, { onSuccess: setResult });
  }

  return (
    <Dialog open onClose={onClose} titleId="replay-dialog-title" className="max-w-2xl">
      <DialogHeader titleId="replay-dialog-title">
        Replay run <code className="font-mono">{run.id}</code>
      </DialogHeader>

      {result ? (
        <div className="space-y-3">
          <p className="text-sm text-text-primary">
            Queued replay <code className="text-mono-chip">{result.run.id}</code>.
          </p>
          <div>
            <h3 className="text-micro text-text-tertiary mb-1">Command</h3>
            <div className="flex items-start gap-2">
              <code className="flex-1 font-mono text-xs p-2 rounded-lg border border-border bg-bg-primary break-all">
                {result.command}
              </code>
              <CopyButton text={result.command} />
            </div>
          </div>
          <div>
            <h3 className="text-micro text-text-tertiary mb-1">CI snippet</h3>
            <pre className="text-xs p-2 rounded-lg border border-border overflow-x-auto bg-bg-primary whitespace-pre-wrap">
              {result.ci_hint}
            </pre>
          </div>
          <DialogFooter>
            <Button onClick={onClose}>Done</Button>
          </DialogFooter>
        </div>
      ) : (
        <>
          <p className="text-xs text-text-tertiary -mt-1 mb-3">
            Leave a field blank to copy it from the parent run.
          </p>
          <div className="space-y-3">
            <Input
              title="Persona"
              placeholder={run.persona_name}
              value={personaName}
              maxLength={50}
              onChange={(e) => setPersonaName(e.target.value)}
            />
            <Input
              title="Ticket"
              placeholder={run.ticket_id ?? 'none'}
              value={ticketId}
              maxLength={64}
              onChange={(e) => setTicketId(e.target.value)}
            />
            <Textarea
              title="Prompt"
              value={prompt}
              maxLength={20000}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="(inherit)"
            />
            <Textarea
              title="Context refs (one per line)"
              value={contextRefs}
              onChange={(e) => setContextRefs(e.target.value)}
              placeholder={run.context_refs.join('\n') || '(inherit)'}
            />
            <Select
              title="Fail-on"
              value={failOn}
              onValueChange={setFailOn}
              options={FAIL_ON_OPTIONS}
            />
            <Input
              title="Trigger ref"
              placeholder={run.trigger_ref ?? '(inherit)'}
              value={triggerRef}
              maxLength={200}
              onChange={(e) => setTriggerRef(e.target.value)}
            />
          </div>
          {replay.error && (
            <p role="alert" className="text-danger text-sm mt-3">{errorMessage(replay.error)}</p>
          )}
          <DialogFooter>
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
            <Button onClick={submit} loading={replay.isPending}>Replay</Button>
          </DialogFooter>
        </>
      )}
    </Dialog>
  );
}

function findingLabel(f: Record<string, unknown>): string {
  const rule = f.rule;
  const title = f.title;
  if (typeof rule === 'string' && rule) return rule;
  if (typeof title === 'string' && title) return title;
  return '(unnamed finding)';
}

function FindingList({ label, items }: { label: string; items: Record<string, unknown>[] }) {
  if (items.length === 0) {
    return <p className="text-xs text-text-tertiary">No {label.toLowerCase()} findings.</p>;
  }
  return (
    <div>
      <h3 className="text-micro text-text-tertiary mb-1">
        {label} ({items.length})
      </h3>
      <ul className="list-disc list-inside">
        {items.map((f, i) => (
          <li key={`${label}-${i}`} className="text-xs text-text-secondary font-mono">
            {findingLabel(f)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function CompareDialog({
  projectId,
  baseRun,
  otherRuns,
  onClose,
}: {
  projectId: string;
  baseRun: AgentRun;
  otherRuns: AgentRun[];
  onClose: () => void;
}) {
  const [otherId, setOtherId] = useState('');
  const { data, isFetching, error } = useCompareAgentRuns(
    projectId,
    baseRun.id,
    otherId || undefined,
  );

  return (
    <Dialog open onClose={onClose} titleId="compare-dialog-title" className="max-w-2xl">
      <DialogHeader titleId="compare-dialog-title">Compare findings</DialogHeader>
      <div className="space-y-3">
        <p className="text-sm text-text-secondary">
          Base run <code className="text-mono-chip">{baseRun.id}</code>
        </p>
        <Select
          aria-label="Compare with run"
          value={otherId}
          onValueChange={setOtherId}
          options={otherRuns.map((r) => ({ value: r.id, label: r.id }))}
          placeholder="Choose another run…"
        />
        {error && <p role="alert" className="text-danger text-sm">{errorMessage(error)}</p>}
        {isFetching && <p className="text-sm text-text-tertiary">Comparing…</p>}
        {data && !isFetching && (
          <div className="space-y-3 border-t border-border pt-3">
            <div className="flex gap-4 flex-wrap text-xs">
              <span className="text-text-secondary">
                {baseRun.id}: {data.a_findings_count} findings
                {Object.entries(data.a_severity_counts).map(([sev, n]) => (
                  <span key={sev} className="ml-1 text-text-tertiary">{sev} {n}</span>
                ))}
              </span>
              <span className="text-text-secondary">
                {data.b_id}: {data.b_findings_count} findings
                {Object.entries(data.b_severity_counts).map(([sev, n]) => (
                  <span key={sev} className="ml-1 text-text-tertiary">{sev} {n}</span>
                ))}
              </span>
            </div>
            <FindingList label="Added" items={data.added} />
            <FindingList label="Removed" items={data.removed} />
            <FindingList label="Unchanged" items={data.unchanged} />
            {data.truncated && (
              <p className="text-xs text-text-tertiary">
                Diff was truncated to fit the response.
              </p>
            )}
          </div>
        )}
      </div>
      <DialogFooter>
        <Button variant="secondary" onClick={onClose}>Close</Button>
      </DialogFooter>
    </Dialog>
  );
}
