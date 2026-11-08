import { useMemo, useState } from 'react';
import { Alert, Badge, Button, Card, Checkbox, Dialog, DialogFooter, DialogHeader, MonoText, Select, Table, Textarea } from '../../components/ui';
import type { EvalResultVerdict, EvalRun, EvalRunState, EvalRunVerdict } from '../../api/client';
import { useEvalResults, useEvalRun, useEvalRuns, useEvalSuites, useVoidEvalRun } from '../../hooks/useEvals';
import { errorMessage } from '../../utils/errorMessage';
import { pct, shortDate, subjectOf } from './evalUtils';
import { ResultVerdictBadge, RunVerdictBadge, TrustBadge } from './format';

const STATE_OPTIONS = [
  { value: '', label: 'Any state' },
  { value: 'running', label: 'Running' },
  { value: 'finished', label: 'Finished' },
  { value: 'voided', label: 'Voided' },
  { value: 'expired', label: 'Expired' },
];
const VERDICT_OPTIONS = [
  { value: '', label: 'Any verdict' },
  { value: 'passed', label: 'Passed' },
  { value: 'failed', label: 'Failed' },
  { value: 'incomplete', label: 'Incomplete' },
];

export function RunsList({
  projectId,
  onOpen,
  onCompare,
}: {
  projectId: string;
  onOpen: (id: string) => void;
  onCompare: (a: string, b: string) => void;
}) {
  const [suite, setSuite] = useState('');
  const [state, setState] = useState('');
  const [verdict, setVerdict] = useState('');
  const [picked, setPicked] = useState<string[]>([]);
  const suites = useEvalSuites(projectId);
  const suiteByLineage = useMemo(() => new Map((suites.data?.suites ?? []).map((s) => [s.lineage_id, s])), [suites.data]);
  const q = useEvalRuns(projectId, {
    suite: suite || undefined,
    state: (state || undefined) as EvalRunState | undefined,
    verdict: (verdict || undefined) as EvalRunVerdict | undefined,
  });
  const runs = useMemo(() => (q.data?.pages ?? []).flatMap((p) => p.runs), [q.data]);

  const toggle = (id: string) =>
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : cur.length >= 2 ? [cur[1], id] : [...cur, id]));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2" role="group" aria-label="Filter runs">
        <div className="w-full sm:w-48">
          <Select
            aria-label="Suite"
            value={suite}
            onValueChange={setSuite}
            options={[{ value: '', label: 'All suites' }, ...(suites.data?.suites ?? []).map((s) => ({ value: s.lineage_id, label: s.name }))]}
          />
        </div>
        <div className="w-[calc(50%-0.25rem)] sm:w-36">
          <Select aria-label="State" value={state} onValueChange={setState} options={STATE_OPTIONS} />
        </div>
        <div className="w-[calc(50%-0.25rem)] sm:w-36">
          <Select aria-label="Verdict" value={verdict} onValueChange={setVerdict} options={VERDICT_OPTIONS} />
        </div>
        <div className="flex flex-1 items-center justify-end gap-2">
          <span className="text-xs text-text-tertiary" aria-live="polite">
            {picked.length === 0 ? 'Pick two runs to compare' : `${picked.length} of 2 picked`}
          </span>
          <Button size="sm" variant="secondary" disabled={picked.length !== 2} onClick={() => onCompare(picked[0], picked[1])}>
            Compare
          </Button>
        </div>
      </div>
      {q.error && <Alert variant="danger" title="Couldn't load runs">{errorMessage(q.error)}</Alert>}
      {q.isLoading ? (
        <p className="text-sm text-text-tertiary">Loading runs…</p>
      ) : (
        <Table<EvalRun>
          label="Eval runs"
          minWidth={760}
          rowKey={(r) => r.id}
          data={runs}
          emptyState={
            suite || state || verdict ? 'No runs match these filters.' : (
              <span>No runs yet. A grader starts one with <code className="font-mono">sfs eval run start</code> or the start_eval_run tool.</span>
            )
          }
          columns={[
            {
              key: 'pick',
              header: <span className="sr-only">Compare</span>,
              width: 'w-10',
              render: (r) => (
                <Checkbox
                  aria-label={`Pick ${r.id} to compare`}
                  checked={picked.includes(r.id)}
                  onChange={() => toggle(r.id)}
                />
              ),
            },
            {
              key: 'run',
              header: 'Run',
              render: (r) => (
                <button
                  type="button"
                  onClick={() => onOpen(r.id)}
                  className="text-left focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                >
                  <span className="block font-medium text-text-primary underline-offset-2 hover:underline">
                    {suiteByLineage.get(r.suite_lineage_id)?.name ?? r.suite?.name ?? r.suite_lineage_id}
                  </span>
                  <span className="block font-mono text-2xs text-text-muted">{r.id}</span>
                </button>
              ),
            },
            { key: 'subject', header: 'Subject', render: (r) => <span className="break-all font-mono text-xs text-text-secondary">{subjectOf(r)}</span> },
            {
              key: 'verdict',
              header: 'Verdict',
              render: (r) => (
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  <RunVerdictBadge verdict={r.verdict} state={r.state} />
                  <TrustBadge run={r} />
                </span>
              ),
            },
            { key: 'rate', header: 'Pass rate', align: 'right', render: (r) => pct(r.summary?.pass_rate) },
            { key: 'started', header: 'Started', render: (r) => shortDate(r.started_at) },
          ]}
        />
      )}
      {q.hasNextPage && (
        <Button variant="secondary" size="sm" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>
          Load more runs
        </Button>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-bg-sunken px-3 py-2">
      <p className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">{label}</p>
      <p className="text-md font-semibold tabular-nums text-text-primary">{value}</p>
    </div>
  );
}

export function RunDetail({
  projectId,
  runId,
  canAdmin,
  onBack,
}: {
  projectId: string;
  runId: string;
  canAdmin: boolean;
  onBack: () => void;
}) {
  const { data: run, isLoading, error } = useEvalRun(projectId, runId);
  const [voiding, setVoiding] = useState(false);
  const back = (
    <Button variant="ghost" size="sm" onClick={onBack}>
      ← All runs
    </Button>
  );
  if (isLoading) return <div className="space-y-2">{back}<p className="text-sm text-text-tertiary">Loading run…</p></div>;
  if (error || !run) return <div className="space-y-2">{back}<Alert variant="danger" title="Couldn't load this run">{errorMessage(error, 'Run not found')}</Alert></div>;
  const s = run.summary;
  const untrusted = run.state === 'finished' && !run.trusted;
  return (
    <div className="space-y-4" data-testid="eval-run-detail">
      {back}
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-md font-semibold text-text-primary">
            {run.suite ? `${run.suite.name} v${run.suite.version}` : run.suite_lineage_id}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <RunVerdictBadge verdict={run.verdict} state={run.state} />
            <TrustBadge run={run} />
            <span className="font-mono text-2xs text-text-muted">{run.id}</span>
          </div>
        </div>
        {canAdmin && run.state !== 'voided' && (
          <Button variant="secondary" size="sm" onClick={() => setVoiding(true)}>
            Void run
          </Button>
        )}
      </div>
      {untrusted && (
        <Alert variant="warning" title="Unverified run">
          {run.untrusted_reason ?? 'The grader is not a trusted, independent reviewer for this suite.'} An unverified run never satisfies a promotion gate.
        </Alert>
      )}
      {run.state === 'voided' && (
        <Alert variant="info" title="Voided">
          {run.voided_reason ?? 'No reason recorded.'} The run stays on record and no longer counts for gates or baselines.
        </Alert>
      )}
      {run.state === 'expired' && (
        <Alert variant="info" title="Expired">This run was never finished within 7 days. It has no verdict.</Alert>
      )}
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
        <dt className="text-text-tertiary">Subject</dt>
        <dd className="min-w-0 break-all font-mono text-xs text-text-primary">{subjectOf(run)}</dd>
        {run.commit_sha && (<><dt className="text-text-tertiary">Commit</dt><dd className="min-w-0"><MonoText value={run.commit_sha} tailChars={8} copyable /></dd></>)}
        <dt className="text-text-tertiary">Grader</dt>
        <dd className="min-w-0 break-words text-text-primary">
          {run.grader_persona}
          {run.service_key_name ? ` · key ${run.service_key_name}` : run.actor_type === 'user' ? ' · person' : ''}
        </dd>
        <dt className="text-text-tertiary">Trigger</dt>
        <dd className="min-w-0 break-words text-text-primary">{run.trigger_source}{run.trigger_ref ? ` · ${run.trigger_ref}` : ''}</dd>
        <dt className="text-text-tertiary">Finished</dt>
        <dd className="text-text-primary">{run.finished_at ? shortDate(run.finished_at) : 'Not yet'}</dd>
      </dl>
      {s && (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Pass rate" value={pct(s.pass_rate)} />
            <Stat label="Passed" value={`${s.passed} / ${s.total_cases}`} />
            <Stat label="Mean score" value={s.mean_score == null ? '—' : s.mean_score.toFixed(2)} />
            <Stat label="Regressions" value={s.regression_count} />
          </div>
          {(s.failed > 0 || s.error > 0 || s.skip > 0 || s.missing > 0) && (
            <p className="text-xs text-text-tertiary">
              {s.failed} failed · {s.error} errored · {s.skip} skipped · {s.missing} without a result
            </p>
          )}
          {s.reasons.length > 0 && (
            <Card className="p-4">
              <h4 className="text-sm font-semibold text-text-primary">Why this verdict</h4>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-text-secondary">
                {s.reasons.map((r) => <li key={r}>{r}</li>)}
              </ul>
              {s.regressions.length > 0 && (
                <p className="mt-2 text-xs text-text-tertiary">
                  Regressed cases: <span className="font-mono">{s.regressions.join(', ')}</span>
                </p>
              )}
            </Card>
          )}
          {Object.keys(s.by_tag).length > 0 && (
            <Table
              label="Pass rate by tag"
              rowKey={(row: [string, (typeof s.by_tag)[string]]) => row[0]}
              data={Object.entries(s.by_tag)}
              columns={[
                { key: 'tag', header: 'Tag', render: ([t]) => <Badge variant={t === 'critical' ? 'danger' : 'default'} tint label={t} /> },
                { key: 'graded', header: 'Graded', align: 'right', render: ([, v]) => `${v.graded} / ${v.cases}` },
                { key: 'passed', header: 'Passed', align: 'right', render: ([, v]) => v.passed },
                { key: 'rate', header: 'Pass rate', align: 'right', render: ([, v]) => pct(v.pass_rate) },
              ]}
            />
          )}
        </>
      )}
      <RunResults projectId={projectId} runId={run.id} tags={s ? Object.keys(s.by_tag) : []} />
      {voiding && <VoidRunDialog projectId={projectId} run={run} onClose={() => setVoiding(false)} />}
    </div>
  );
}

const RESULT_OPTIONS = [
  { value: '', label: 'All results' },
  { value: 'pass', label: 'Pass' },
  { value: 'fail', label: 'Fail' },
  { value: 'error', label: 'Error' },
  { value: 'skip', label: 'Skip' },
];

function RunResults({ projectId, runId, tags }: { projectId: string; runId: string; tags: string[] }) {
  const [verdict, setVerdict] = useState('');
  const [tag, setTag] = useState('');
  const q = useEvalResults(projectId, runId, {
    verdict: (verdict || undefined) as EvalResultVerdict | undefined,
    tag: tag || undefined,
  });
  const rows = useMemo(() => (q.data?.pages ?? []).flatMap((p) => p.results), [q.data]);
  const notice = q.data?.pages[0]?.untrusted_content_notice;
  return (
    <section aria-labelledby="run-results-heading" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h4 id="run-results-heading" className="flex-1 text-sm font-semibold text-text-primary">Case results</h4>
        <div className="w-36">
          <Select aria-label="Filter by result" value={verdict} onValueChange={setVerdict} options={RESULT_OPTIONS} />
        </div>
        {tags.length > 0 && (
          <div className="w-36">
            <Select aria-label="Filter by tag" value={tag} onValueChange={setTag} options={[{ value: '', label: 'All tags' }, ...tags.map((t) => ({ value: t, label: t }))]} />
          </div>
        )}
      </div>
      {notice && <p className="text-xs text-text-tertiary">{notice}</p>}
      {q.isLoading && <p className="text-sm text-text-tertiary">Loading results…</p>}
      {q.error && <Alert variant="danger" title="Couldn't load results">{errorMessage(q.error)}</Alert>}
      {!q.isLoading && !q.error && rows.length === 0 && <p className="text-sm text-text-tertiary">No results{verdict || tag ? ' match these filters' : ' yet'}.</p>}
      {rows.length > 0 && (
        <ul className="divide-y divide-border rounded-md border border-border" aria-label="Results">
          {rows.map((r) => (
            <li key={r.id} className="px-3 py-2">
              <details className="min-w-0">
                <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 rounded focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
                  <ResultVerdictBadge verdict={r.verdict} />
                  <span className="min-w-0 break-all font-mono text-xs text-text-primary">{r.case_key}</span>
                  {r.score != null && <span className="text-xs tabular-nums text-text-tertiary">score {r.score.toFixed(2)}</span>}
                </summary>
                <div className="mt-2 space-y-2">
                  {r.output_cleared ? (
                    <p className="text-xs text-text-tertiary">Output and notes were cleared by the retention window. The verdict, score and hash stay.</p>
                  ) : (
                    <>
                      {r.notes && <p className="whitespace-pre-wrap break-words text-xs text-text-secondary">{r.notes}</p>}
                      {r.output && (
                        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg-sunken p-2 text-xs text-text-secondary">{r.output}</pre>
                      )}
                    </>
                  )}
                  {r.output_sha256 && (
                    <p className="flex min-w-0 items-center gap-2 text-2xs text-text-muted">
                      Output sha256 <MonoText value={r.output_sha256} tailChars={10} className="min-w-0" />
                    </p>
                  )}
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
      {q.hasNextPage && (
        <Button variant="secondary" size="sm" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>
          Load more results
        </Button>
      )}
    </section>
  );
}

export function VoidRunDialog({ projectId, run, onClose }: { projectId: string; run: EvalRun; onClose: () => void }) {
  const [reason, setReason] = useState('');
  const [touched, setTouched] = useState(false);
  const mutation = useVoidEvalRun(projectId);
  const empty = reason.trim().length === 0;
  const submit = () => {
    setTouched(true);
    if (empty) return;
    mutation.mutate({ runId: run.id, reason: reason.trim() }, { onSuccess: onClose });
  };
  return (
    <Dialog open onClose={() => !mutation.isPending && onClose()} titleId="void-run-title">
      <DialogHeader titleId="void-run-title">Void this run?</DialogHeader>
      <p className="mb-3 text-sm text-text-tertiary">
        The run stays on record with your reason, but it stops counting for promotion gates and regression baselines. This can't be undone; a correction is a new run.
      </p>
      <label htmlFor="void-reason" className="text-sm font-medium text-text-secondary">Reason</label>
      <Textarea
        id="void-reason"
        value={reason}
        maxLength={500}
        onChange={(e) => setReason(e.target.value)}
        error={touched && empty ? 'Give a reason for voiding the run.' : undefined}
        className="mt-1"
      />
      {mutation.error && <Alert variant="danger" className="mt-3">{errorMessage(mutation.error, "Couldn't void the run")}</Alert>}
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={mutation.isPending}>Cancel</Button>
        <Button variant="danger" onClick={submit} loading={mutation.isPending} disabled={mutation.isPending}>
          Void run
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
