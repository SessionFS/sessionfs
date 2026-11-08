import { useMemo } from 'react';
import { Alert, Badge, Card, Select, Table } from '../../components/ui';
import type { EvalCompareSide } from '../../api/client';
import { useEvalCompare, useEvalRuns } from '../../hooks/useEvals';
import { errorMessage } from '../../utils/errorMessage';
import { pct, shortDate, subjectOf } from './evalUtils';
import { RunVerdictBadge } from './format';

const fmtTokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

function SideCard({ label, side, onOpen }: { label: string; side: EvalCompareSide; onOpen: (id: string) => void }) {
  return (
    <Card className="min-w-0 p-4">
      <p className="text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary">{label}</p>
      <button
        type="button"
        onClick={() => onOpen(side.run_id)}
        className="mt-1 break-all text-left font-mono text-xs text-text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        {side.run_id}
      </button>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <RunVerdictBadge verdict={side.verdict} state={side.state} />
        {side.state === 'finished' && <Badge variant={side.trusted ? 'success' : 'warning'} label={side.trusted ? 'Trusted' : 'Unverified'} />}
      </div>
      {side.tokens && (
        <p className="mt-2 text-xs text-text-tertiary">
          {fmtTokens(side.tokens.input)} in · {fmtTokens(side.tokens.output)} out tokens
          {side.cost_usd != null && ` · $${side.cost_usd.toFixed(2)}`}
        </p>
      )}
    </Card>
  );
}

function CaseList({ title, keys, empty }: { title: string; keys: string[]; empty: string }) {
  return (
    <div className="min-w-0">
      <h3 className="text-sm font-semibold text-text-primary">{title} <span className="font-normal text-text-tertiary">({keys.length})</span></h3>
      {keys.length === 0 ? (
        <p className="mt-1 text-xs text-text-tertiary">{empty}</p>
      ) : (
        <ul className="mt-1 space-y-0.5">
          {keys.map((k) => <li key={k} className="break-all font-mono text-xs text-text-secondary">{k}</li>)}
        </ul>
      )}
    </div>
  );
}

/** Two runs of one suite lineage, matched by case key. Untrusted sides are called out first. */
export function CompareView({
  projectId,
  a,
  b,
  onChange,
  onOpenRun,
}: {
  projectId: string;
  a: string | null;
  b: string | null;
  onChange: (a: string | null, b: string | null) => void;
  onOpenRun: (id: string) => void;
}) {
  const runsQ = useEvalRuns(projectId, { state: 'finished' });
  const runs = useMemo(() => (runsQ.data?.pages ?? []).flatMap((p) => p.runs), [runsQ.data]);
  const options = [
    { value: '', label: 'Pick a run' },
    ...runs.map((r) => ({ value: r.id, label: `${r.suite?.name ?? r.suite_lineage_id} · ${subjectOf(r)} · ${shortDate(r.started_at)}` })),
  ];
  // A run picked from the list may not be in the first page; keep it selectable.
  for (const id of [a, b]) if (id && !options.some((o) => o.value === id)) options.push({ value: id, label: id });
  const q = useEvalCompare(projectId, a, b);
  const c = q.data;
  return (
    <div className="space-y-4" data-testid="eval-compare">
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="min-w-0">
          <Select title="Baseline (A)" value={a ?? ''} onValueChange={(v) => onChange(v || null, b)} options={options} />
        </div>
        <div className="min-w-0">
          <Select title="Candidate (B)" value={b ?? ''} onValueChange={(v) => onChange(a, v || null)} options={options} />
        </div>
      </div>
      {(!a || !b) && <p className="text-sm text-text-tertiary">Pick two runs of the same suite. Suite versions may differ; cases are matched by key.</p>}
      {a && b && a === b && <p className="text-sm text-text-tertiary">Pick two different runs.</p>}
      {q.isLoading && a && b && a !== b && <p className="text-sm text-text-tertiary">Comparing…</p>}
      {q.error && <Alert variant="danger" title="Couldn't compare these runs">{errorMessage(q.error)}</Alert>}
      {c && (
        <>
          {(c.warnings.length > 0 || !c.a.trusted || !c.b.trusted) && (
            <Alert variant="warning" title="Read this first">
              <ul className="list-disc space-y-0.5 pl-4">
                {c.warnings.map((w) => <li key={w}>{w}</li>)}
                {c.warnings.length === 0 && <li>At least one run is unverified; it doesn't count toward any gate.</li>}
              </ul>
            </Alert>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            <SideCard label="A" side={c.a} onOpen={onOpenRun} />
            <SideCard label="B" side={c.b} onOpen={onOpenRun} />
          </div>
          <Card className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-4">
            <CaseList title="Regressions" keys={c.regressions} empty="Nothing that passed in A fails in B." />
            <CaseList title="Fixes" keys={c.fixes} empty="Nothing that failed in A passes in B." />
            <CaseList title="Added" keys={c.cases_added} empty="Same cases in both versions." />
            <CaseList title="Removed" keys={c.cases_removed} empty="No cases retired." />
            <p className="text-xs text-text-tertiary sm:col-span-2 lg:col-span-4">
              Unchanged: {c.unchanged.pass} passing in both, {c.unchanged.not_pass} not passing in both.
            </p>
          </Card>
          {c.score_deltas.length > 0 && (
            <Table
              label="Score change per case"
              rowKey={(r: (typeof c.score_deltas)[number]) => r.case_key}
              data={c.score_deltas}
              columns={[
                { key: 'case', header: 'Case', render: (r) => <span className="break-all font-mono text-xs">{r.case_key}</span> },
                { key: 'a', header: 'A', align: 'right', render: (r) => r.a.toFixed(2) },
                { key: 'b', header: 'B', align: 'right', render: (r) => r.b.toFixed(2) },
                {
                  key: 'delta',
                  header: 'Change',
                  align: 'right',
                  render: (r) => (
                    <span className={r.delta < 0 ? 'text-danger' : r.delta > 0 ? 'text-success' : 'text-text-tertiary'}>
                      {r.delta > 0 ? '+' : ''}{r.delta.toFixed(2)}
                    </span>
                  ),
                },
              ]}
            />
          )}
          {Object.keys(c.pass_rate_by_tag).length > 0 && (
            <Table
              label="Pass rate by tag"
              rowKey={(r: [string, { a: number | null; b: number | null }]) => r[0]}
              data={Object.entries(c.pass_rate_by_tag)}
              columns={[
                { key: 'tag', header: 'Tag', render: ([t]) => <Badge variant={t === 'critical' ? 'danger' : 'default'} tint label={t} /> },
                { key: 'a', header: 'A', align: 'right', render: ([, v]) => pct(v.a) },
                { key: 'b', header: 'B', align: 'right', render: ([, v]) => pct(v.b) },
              ]}
            />
          )}
        </>
      )}
    </div>
  );
}
