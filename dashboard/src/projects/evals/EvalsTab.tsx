import { useMemo, useState } from 'react';
import { Alert, Badge, Button, Card, MonoText, Select, Table, TabPanel, Tabs } from '../../components/ui';
import type { EvalCase, EvalSuite } from '../../api/client';
import { useEvalSuite, useEvalSuiteCases, useEvalSuites } from '../../hooks/useEvals';
import { errorMessage } from '../../utils/errorMessage';
import { graderLabel, planAllowsEvals, pct, shortDate } from './evalUtils';
import { PlanNotice } from './format';
import { useViewParams } from './params';
import { RunDetail, RunsList } from './EvalRuns';
import { CompareView } from './EvalCompare';

const VIEWS = [
  { key: 'runs', label: 'Runs' },
  { key: 'suites', label: 'Suites' },
  { key: 'compare', label: 'Compare' },
];

/**
 * Project → Work → Evals. Record-only: graders run wherever the team runs
 * them and post results; SessionFS keeps suites, runs and verdicts, says
 * which runs are trusted, and never runs a model.
 */
export default function EvalsTab({
  projectId,
  tier,
  canAdmin,
}: {
  projectId: string;
  tier: string | null | undefined;
  canAdmin: boolean;
}) {
  const v = useViewParams();
  const allowed = planAllowsEvals(tier);
  const view = VIEWS.some((x) => x.key === v.get('ev')) ? (v.get('ev') as string) : 'runs';
  const openRun = v.get('evrun');
  const openSuite = v.get('suite');

  const setView = (key: string) => v.set({ ev: key, evrun: null, suite: null });

  let body;
  if (view === 'runs' && openRun) {
    body = <RunDetail projectId={projectId} runId={openRun} canAdmin={canAdmin} onBack={() => v.set({ evrun: null })} />;
  } else if (view === 'suites' && openSuite) {
    body = <SuiteDetail projectId={projectId} suiteRef={openSuite} onBack={() => v.set({ suite: null })} onSelect={(ref) => v.set({ suite: ref })} />;
  } else if (view === 'suites') {
    body = <SuitesList projectId={projectId} onOpen={(ref) => v.set({ suite: ref })} />;
  } else if (view === 'compare') {
    body = (
      <CompareView
        projectId={projectId}
        a={v.get('a')}
        b={v.get('b')}
        onChange={(a, b) => v.set({ a, b })}
        onOpenRun={(id) => v.set({ ev: 'runs', evrun: id })}
      />
    );
  } else {
    body = (
      <RunsList
        projectId={projectId}
        onOpen={(id) => v.set({ evrun: id })}
        onCompare={(a, b) => v.set({ ev: 'compare', a, b })}
      />
    );
  }

  return (
    <section aria-labelledby="evals-heading" className="space-y-4">
      <div className="min-w-0">
        <h2 id="evals-heading" className="text-md font-semibold text-text-primary">Evaluations</h2>
        <p className="text-sm text-text-tertiary">
          Graders run where you run them and post results here. A run counts for promotion only when it is trusted.
        </p>
      </div>
      <PlanNotice allowed={allowed} what="Recording runs and suites" />
      <Tabs
        bare
        variant="subtle"
        idPrefix="evals-view"
        aria-label="Evaluation views"
        tabs={VIEWS}
        activeKey={view}
        onChange={setView}
      />
      <TabPanel idPrefix="evals-view" tabKey={view}>
        {body}
      </TabPanel>
    </section>
  );
}

function SuitesList({ projectId, onOpen }: { projectId: string; onOpen: (ref: string) => void }) {
  const { data, isLoading, error } = useEvalSuites(projectId);
  if (isLoading) return <p className="text-sm text-text-tertiary">Loading suites…</p>;
  if (error) return <Alert variant="danger" title="Couldn't load suites">{errorMessage(error)}</Alert>;
  const suites = data?.suites ?? [];
  return (
    <Table<EvalSuite>
      label="Eval suites"
      minWidth={640}
      rowKey={(s) => s.id}
      data={suites}
      emptyState={
        <span>
          No suites yet. People define the bar with <code className="font-mono">sfs eval suite create</code>; agents only grade.
        </span>
      }
      columns={[
        {
          key: 'name',
          header: 'Suite',
          render: (s) => (
            <button
              type="button"
              onClick={() => onOpen(s.id)}
              className="min-w-0 text-left font-medium text-text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              {s.name}
              <span className="block font-mono text-2xs text-text-muted">{s.slug}</span>
            </button>
          ),
        },
        { key: 'version', header: 'Version', render: (s) => `v${s.version}` },
        { key: 'grader', header: 'Grader', render: (s) => `${graderLabel(s.grader.kind)} · ${s.grader.persona}` },
        {
          key: 'storage',
          header: 'Cases',
          render: (s) => (
            <span className="inline-flex items-center gap-1.5">
              {s.live_cases ?? '—'}
              {s.case_storage === 'reference' && <Badge variant="info" tint label="Hashes only" />}
            </span>
          ),
        },
        { key: 'created', header: 'Updated', render: (s) => shortDate(s.created_at) },
      ]}
    />
  );
}

function acceptanceLines(s: EvalSuite): string[] {
  const a = s.acceptance;
  const out: string[] = [];
  out.push(a.require_all_cases ? 'Every live case needs a result' : 'Missing results are allowed');
  if (a.min_pass_rate != null) out.push(`Pass rate at least ${pct(a.min_pass_rate)}`);
  if (a.min_mean_score != null) out.push(`Mean score at least ${a.min_mean_score}`);
  for (const [tag, v] of Object.entries(a.min_pass_rate_by_tag ?? {})) out.push(`${tag}: pass rate at least ${pct(v)}`);
  for (const [tag, v] of Object.entries(a.max_failures_by_tag ?? {})) out.push(`${tag}: at most ${v} failure${v === 1 ? '' : 's'}`);
  if (a.no_regression_vs && a.no_regression_vs !== 'none') {
    const vs = a.no_regression_vs === 'production' ? 'the production version' : a.no_regression_vs;
    out.push(`No regression against ${vs} on ${(a.regression_tags ?? []).join(', ') || 'critical'} cases`);
  }
  return out;
}

function SuiteDetail({
  projectId,
  suiteRef,
  onBack,
  onSelect,
}: {
  projectId: string;
  suiteRef: string;
  onBack: () => void;
  onSelect: (ref: string) => void;
}) {
  const { data: s, isLoading, error } = useEvalSuite(projectId, suiteRef);
  const back = (
    <Button variant="ghost" size="sm" onClick={onBack}>
      ← All suites
    </Button>
  );
  if (isLoading) return <div className="space-y-2">{back}<p className="text-sm text-text-tertiary">Loading suite…</p></div>;
  if (error || !s) return <div className="space-y-2">{back}<Alert variant="danger" title="Couldn't load this suite">{errorMessage(error, 'Suite not found')}</Alert></div>;
  const versions = s.versions ?? [];
  return (
    <div className="space-y-4" data-testid="eval-suite-detail">
      {back}
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-md font-semibold text-text-primary">{s.name}</h3>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
            <span className="font-mono">{s.slug}</span>
            <Badge variant={s.active ? 'success' : 'default'} tint label={s.active ? `v${s.version} · active` : `v${s.version} · superseded`} />
            {s.case_storage === 'reference' && <Badge variant="info" tint label="Reference storage" />}
          </div>
        </div>
        {versions.length > 1 && (
          <div className="w-44">
            <Select
              aria-label="Suite version"
              value={s.id}
              onValueChange={onSelect}
              options={versions.map((x) => ({ value: x.id, label: `v${x.version}${x.active ? ' (active)' : ''}` }))}
            />
          </div>
        )}
      </div>
      {s.description && <p className="max-w-3xl text-sm text-text-secondary">{s.description}</p>}
      <div className="grid gap-3 md:grid-cols-2">
        <Card className="min-w-0 p-4">
          <h4 className="text-sm font-semibold text-text-primary">Grader</h4>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-text-tertiary">Kind</dt>
            <dd className="text-text-primary">{graderLabel(s.grader.kind)}</dd>
            <dt className="text-text-tertiary">Agent</dt>
            <dd className="min-w-0 break-words text-text-primary">{s.grader.persona}</dd>
            {s.grader.model_hint && (<><dt className="text-text-tertiary">Model</dt><dd className="min-w-0 break-words text-text-primary">{s.grader.model_hint}</dd></>)}
            {s.grader.script_ref && (<><dt className="text-text-tertiary">Script</dt><dd className="min-w-0 break-all font-mono text-xs text-text-primary">{s.grader.script_ref}</dd></>)}
          </dl>
          {s.grader.rubric && (
            <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg-sunken p-2 text-xs text-text-secondary">{s.grader.rubric}</pre>
          )}
        </Card>
        <Card className="min-w-0 p-4">
          <h4 className="text-sm font-semibold text-text-primary">Passes when</h4>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-text-secondary">
            {acceptanceLines(s).map((l) => <li key={l}>{l}</li>)}
          </ul>
          <p className="mt-3 flex min-w-0 items-center gap-2 text-2xs text-text-muted">
            Definition <MonoText value={s.definition_sha256} tailChars={8} className="min-w-0" />
          </p>
        </Card>
      </div>
      <SuiteCases projectId={projectId} suiteRef={s.id} reference={s.case_storage === 'reference'} />
    </div>
  );
}

function SuiteCases({ projectId, suiteRef, reference }: { projectId: string; suiteRef: string; reference: boolean }) {
  const q = useEvalSuiteCases(projectId, suiteRef);
  const [tag, setTag] = useState('');
  const cases = useMemo(() => (q.data?.pages ?? []).flatMap((p) => p.cases), [q.data]);
  const tags = useMemo(() => Array.from(new Set(cases.flatMap((c) => c.tags))).sort(), [cases]);
  const shown = tag ? cases.filter((c) => c.tags.includes(tag)) : cases;
  const notice = q.data?.pages[0]?.untrusted_content_notice;
  return (
    <section aria-labelledby="suite-cases-heading" className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h4 id="suite-cases-heading" className="flex-1 text-sm font-semibold text-text-primary">Live cases</h4>
        {tags.length > 0 && (
          <div className="w-40">
            <Select aria-label="Filter cases by tag" value={tag} onValueChange={setTag} options={[{ value: '', label: 'All tags' }, ...tags.map((t) => ({ value: t, label: t }))]} />
          </div>
        )}
      </div>
      {reference ? (
        <p className="text-xs text-text-tertiary">
          Reference storage: case text stays with you. SessionFS keeps each case's key and content hash, and a grader proves which case it graded by sending the same hash.
        </p>
      ) : (
        notice && <p className="text-xs text-text-tertiary">{notice}</p>
      )}
      {q.isLoading && <p className="text-sm text-text-tertiary">Loading cases…</p>}
      {q.error && <Alert variant="danger" title="Couldn't load cases">{errorMessage(q.error)}</Alert>}
      {!q.isLoading && !q.error && shown.length === 0 && <p className="text-sm text-text-tertiary">No live cases in this version.</p>}
      <ul className="divide-y divide-border rounded-md border border-border">
        {shown.map((c) => <CaseRow key={c.id} c={c} reference={reference} />)}
      </ul>
      {q.hasNextPage && (
        <Button variant="secondary" size="sm" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>
          Load more cases
        </Button>
      )}
    </section>
  );
}

function CaseRow({ c, reference }: { c: EvalCase; reference: boolean }) {
  const head = (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="font-mono text-xs text-text-primary">{c.case_key}</span>
      <span className="min-w-0 text-sm text-text-secondary">{c.name}</span>
      {c.tags.map((t) => <Badge key={t} variant={t === 'critical' ? 'danger' : 'default'} tint label={t} />)}
      <span className="text-2xs text-text-muted">since v{c.added_in_version}</span>
    </div>
  );
  if (reference) {
    return (
      <li className="space-y-1 px-3 py-2">
        {head}
        <p className="flex min-w-0 items-center gap-2 text-2xs text-text-muted">
          sha256 <MonoText value={c.content_sha256} tailChars={10} copyable className="min-w-0" />
        </p>
      </li>
    );
  }
  return (
    <li className="px-3 py-2">
      <details className="group min-w-0">
        <summary className="cursor-pointer list-none rounded focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
          {head}
        </summary>
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          <div className="min-w-0">
            <p className="text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary">Input</p>
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg-sunken p-2 text-xs text-text-secondary">{c.input ?? '—'}</pre>
          </div>
          <div className="min-w-0">
            <p className="text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary">Expected</p>
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg-sunken p-2 text-xs text-text-secondary">{c.expected ?? '—'}</pre>
          </div>
          {c.rubric_notes && <p className="text-xs text-text-tertiary md:col-span-2">Rubric note: {c.rubric_notes}</p>}
        </div>
      </details>
    </li>
  );
}
