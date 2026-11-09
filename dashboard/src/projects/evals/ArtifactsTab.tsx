import { useMemo, useState } from 'react';
import { Alert, Badge, Button, Card, Checkbox, Dialog, DialogFooter, DialogHeader, Input, MonoText, Select, Table } from '../../components/ui';
import { ApiError, type Artifact, type ArtifactFamily, type ArtifactKind, type ArtifactStage, type PromotionBlocker } from '../../api/client';
import {
  useArtifact,
  useArtifactFamilies,
  useArtifactLineage,
  useArtifacts,
  useDeprecateArtifact,
  useEvalSuites,
  useFamilyEvals,
  useFamilyRequirements,
  usePromoteArtifact,
  usePromotionCheck,
  useSaveFamilyRequirements,
  useSuiteNames,
} from '../../hooks/useEvals';
import { errorMessage } from '../../utils/errorMessage';
import { artifactLabel, KIND_LABEL, pct, planAllowsEvals, shortDate, stageLabel, STAGES } from './evalUtils';
import { ArtifactUri, PlanNotice, RunVerdictBadge, StageBadge } from './format';
import { useViewParams } from './params';
import { LineageGraph } from './LineageGraph';
import { Term } from '../../help/Term';

const KIND_OPTIONS = [{ value: '', label: 'All kinds' }, ...Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))];
const STAGE_OPTIONS = [{ value: '', label: 'Any stage' }, ...STAGES.map((s) => ({ value: s, label: stageLabel(s) }))];

const familyKey = (kind: string, name: string) => `${kind}:${name}`;
function parseFamily(v: string | null): { kind: ArtifactKind; name: string } | null {
  if (!v) return null;
  const i = v.indexOf(':');
  if (i <= 0) return null;
  const kind = v.slice(0, i);
  if (!(kind in KIND_LABEL)) return null;
  return { kind: kind as ArtifactKind, name: v.slice(i + 1) };
}

interface Props {
  projectId: string;
  tier: string | null | undefined;
  canEdit: boolean;
  canAdmin: boolean;
  /** Open an eval run on the Evals sub-tab. */
  onOpenRun: (runId: string) => void;
}

/**
 * Project → Work → Artifacts. Pointers to datasets, models, prompts and
 * checkpoints that live elsewhere (SessionFS keeps the URI and digest, never
 * the bytes), their lineage, and the eval-gated promotion through stages.
 */
export default function ArtifactsTab(props: Props) {
  const v = useViewParams();
  const allowed = planAllowsEvals(props.tier);
  const art = v.get('art');
  const family = parseFamily(v.get('family'));
  let body;
  if (art) {
    body = (
      <ArtifactDetail
        {...props}
        allowed={allowed}
        artifactId={art}
        onBack={() => v.set({ art: null })}
        onOpen={(id) => v.set({ art: id, family: null })}
        onOpenFamily={(k, n) => v.set({ art: null, family: familyKey(k, n) })}
      />
    );
  } else if (family) {
    body = (
      <FamilyView
        {...props}
        allowed={allowed}
        kind={family.kind}
        name={family.name}
        onBack={() => v.set({ family: null })}
        onOpen={(id) => v.set({ art: id, family: null })}
      />
    );
  } else {
    body = <ArtifactsList projectId={props.projectId} onOpen={(id) => v.set({ art: id })} onOpenFamily={(k, n) => v.set({ family: familyKey(k, n) })} />;
  }
  return (
    <section aria-labelledby="artifacts-heading" className="space-y-4">
      <div className="min-w-0">
        <h2 id="artifacts-heading" className="text-md font-semibold text-text-primary">
          <Term id="artifact">Artifacts</Term>
        </h2>
        <p className="text-sm text-text-tertiary">
          Datasets, models and prompts by version, what each came from, and which evals it has passed. SessionFS stores pointers, never the files.
        </p>
      </div>
      <PlanNotice allowed={allowed} what="Promotion and family requirements" />
      {body}
    </section>
  );
}

function ArtifactsList({
  projectId,
  onOpen,
  onOpenFamily,
}: {
  projectId: string;
  onOpen: (id: string) => void;
  onOpenFamily: (kind: ArtifactKind, name: string) => void;
}) {
  const [kind, setKind] = useState('');
  const [stage, setStage] = useState('');
  const [deprecated, setDeprecated] = useState(false);
  const families = useArtifactFamilies(projectId);
  const q = useArtifacts(projectId, {
    kind: (kind || undefined) as ArtifactKind | undefined,
    stage: (stage || undefined) as ArtifactStage | undefined,
    include_deprecated: deprecated || undefined,
  });
  const rows = useMemo(() => (q.data?.pages ?? []).flatMap((p) => p.artifacts), [q.data]);
  const fams = families.data?.families ?? [];
  return (
    <div className="space-y-5">
      <section aria-labelledby="families-heading" className="space-y-2">
        <h3 id="families-heading" className="text-sm font-semibold text-text-primary">Families</h3>
        {families.error && <Alert variant="danger" title="Couldn't load families">{errorMessage(families.error)}</Alert>}
        {families.isLoading ? (
          <p className="text-sm text-text-tertiary">Loading families…</p>
        ) : fams.length === 0 ? (
          <p className="text-sm text-text-tertiary">
            No artifacts yet. Register one with <code className="font-mono">sfs artifact register</code> or the register_artifact tool.
          </p>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {fams.map((f: ArtifactFamily) => (
              <li key={familyKey(f.kind, f.name)}>
                <button
                  type="button"
                  onClick={() => onOpenFamily(f.kind, f.name)}
                  className="w-full min-w-0 rounded-lg border border-border bg-surface p-3 text-left transition-colors hover:bg-surface-hover focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                >
                  <span className="block break-all text-sm font-medium text-text-primary">{f.name}</span>
                  <span className="mt-0.5 block text-xs text-text-tertiary">
                    {KIND_LABEL[f.kind] ?? f.kind} · {f.versions} version{f.versions === 1 ? '' : 's'}
                    {f.production_version ? ` · production ${f.production_version}` : ' · nothing in production'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="versions-heading" className="space-y-2">
        <div className="flex flex-wrap items-end gap-2">
          <h3 id="versions-heading" className="w-full text-sm font-semibold text-text-primary sm:w-auto sm:flex-1">Versions</h3>
          <div className="w-[calc(50%-0.25rem)] sm:w-36">
            <Select aria-label="Kind" value={kind} onValueChange={setKind} options={KIND_OPTIONS} />
          </div>
          <div className="w-[calc(50%-0.25rem)] sm:w-36">
            <Select aria-label="Stage" value={stage} onValueChange={setStage} options={STAGE_OPTIONS} />
          </div>
          <Checkbox label="Include deprecated" checked={deprecated} onChange={(e) => setDeprecated(e.target.checked)} />
        </div>
        {q.error && <Alert variant="danger" title="Couldn't load artifacts">{errorMessage(q.error)}</Alert>}
        {q.isLoading ? (
          <p className="text-sm text-text-tertiary">Loading artifacts…</p>
        ) : (
          <Table<Artifact>
            label="Artifact versions"
            minWidth={640}
            rowKey={(a) => a.id}
            data={rows}
            emptyState={kind || stage ? 'No artifacts match these filters.' : 'No artifacts yet.'}
            columns={[
              {
                key: 'name',
                header: 'Artifact',
                render: (a) => (
                  <button
                    type="button"
                    onClick={() => onOpen(a.id)}
                    className="min-w-0 text-left focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  >
                    <span className="block break-all font-medium text-text-primary underline-offset-2 hover:underline">{artifactLabel(a)}</span>
                    <span className="block text-2xs text-text-muted">{KIND_LABEL[a.kind] ?? a.kind}</span>
                  </button>
                ),
              },
              { key: 'stage', header: <Term id="artifact-stage">Stage</Term>, render: (a) => <StageBadge stage={a.stage} deprecated={!!a.deprecated_at} /> },
              { key: 'tags', header: 'Tags', render: (a) => <TagList tags={a.tags} /> },
              { key: 'created', header: 'Registered', render: (a) => shortDate(a.created_at) },
            ]}
          />
        )}
        {q.hasNextPage && (
          <Button variant="secondary" size="sm" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>
            Load more
          </Button>
        )}
      </section>
    </div>
  );
}

function TagList({ tags }: { tags: Record<string, string> }) {
  const entries = Object.entries(tags);
  if (entries.length === 0) return <span className="text-text-muted">—</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {entries.map(([k, val]) => <Badge key={k} variant="default" tint label={`${k}=${val}`} />)}
    </span>
  );
}

function ArtifactDetail({
  projectId,
  artifactId,
  allowed,
  canEdit,
  onBack,
  onOpen,
  onOpenFamily,
  onOpenRun,
}: Props & {
  artifactId: string;
  allowed: boolean;
  onBack: () => void;
  onOpen: (id: string) => void;
  onOpenFamily: (kind: ArtifactKind, name: string) => void;
}) {
  const { data: a, isLoading, error } = useArtifact(projectId, artifactId);
  const lineage = useArtifactLineage(projectId, artifactId);
  const suiteNames = useSuiteNames(projectId);
  const [confirmDeprecate, setConfirmDeprecate] = useState(false);
  const back = (
    <Button variant="ghost" size="sm" onClick={onBack}>
      ← All artifacts
    </Button>
  );
  if (isLoading) return <div className="space-y-2">{back}<p className="text-sm text-text-tertiary">Loading artifact…</p></div>;
  if (error || !a) return <div className="space-y-2">{back}<Alert variant="danger" title="Couldn't load this artifact">{errorMessage(error, 'Artifact not found')}</Alert></div>;
  return (
    <div className="space-y-4" data-testid="artifact-detail">
      {back}
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="break-all text-md font-semibold text-text-primary">{artifactLabel(a)}</h3>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
            <span>{KIND_LABEL[a.kind] ?? a.kind}</span>
            <StageBadge stage={a.stage} deprecated={!!a.deprecated_at} />
            <button
              type="button"
              onClick={() => onOpenFamily(a.kind, a.name)}
              className="text-brand underline-offset-2 hover:underline focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              All versions × suites
            </button>
          </div>
        </div>
        {canEdit && !a.deprecated_at && (
          <Button variant="secondary" size="sm" onClick={() => setConfirmDeprecate(true)}>
            Deprecate
          </Button>
        )}
      </div>
      <Card className="min-w-0 space-y-3 p-4">
        <ArtifactUri artifact={a} />
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
          {a.digest && (<><dt className="text-text-tertiary">Digest</dt><dd className="min-w-0"><MonoText value={a.digest} tailChars={10} copyable /></dd></>)}
          <dt className="text-text-tertiary">Tags</dt>
          <dd className="min-w-0"><TagList tags={a.tags} /></dd>
          {a.produced_by_agent_run_id && (<><dt className="text-text-tertiary">Produced by</dt><dd className="min-w-0 break-all font-mono text-xs text-text-primary">{a.produced_by_agent_run_id}</dd></>)}
          <dt className="text-text-tertiary">Registered</dt>
          <dd className="text-text-primary">{shortDate(a.created_at)}{a.service_key_name ? ` by key ${a.service_key_name}` : ''}</dd>
          {a.deprecated_at && (<><dt className="text-text-tertiary">Deprecated</dt><dd className="text-text-primary">{shortDate(a.deprecated_at)}</dd></>)}
        </dl>
        {a.description && <p className="text-sm text-text-secondary">{a.description}</p>}
      </Card>
      <PromotionPanel projectId={projectId} artifact={a} canPromote={canEdit && allowed} />
      <section aria-labelledby="lineage-heading" className="space-y-2">
        <h4 id="lineage-heading" className="text-sm font-semibold text-text-primary">
          <Term id="lineage">Lineage</Term>
        </h4>
        {lineage.isLoading && <p className="text-sm text-text-tertiary">Tracing lineage…</p>}
        {lineage.error && <Alert variant="danger" title="Couldn't trace lineage">{errorMessage(lineage.error)}</Alert>}
        {lineage.data && <LineageGraph lineage={lineage.data} onOpen={onOpen} />}
      </section>
      <section aria-labelledby="trusted-runs-heading" className="space-y-2">
        <h4 id="trusted-runs-heading" className="text-sm font-semibold text-text-primary">Latest trusted runs</h4>
        {(a.latest_trusted_runs ?? []).length === 0 ? (
          <p className="text-sm text-text-tertiary">No trusted run on this version yet. Only trusted runs count for promotion.</p>
        ) : (
          <ul className="space-y-1">
            {(a.latest_trusted_runs ?? []).map((r) => (
              <li key={r.run_id} className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
                <RunVerdictBadge verdict={r.verdict} state="finished" />
                <button
                  type="button"
                  onClick={() => onOpenRun(r.run_id)}
                  className="text-left text-text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                >
                  {suiteNames.get(r.suite_lineage_id)?.name ?? r.suite_lineage_id}
                </button>
                <span className="text-xs text-text-tertiary">{shortDate(r.finished_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
      {confirmDeprecate && <DeprecateDialog projectId={projectId} artifact={a} onClose={() => setConfirmDeprecate(false)} />}
    </div>
  );
}

function DeprecateDialog({ projectId, artifact, onClose }: { projectId: string; artifact: Artifact; onClose: () => void }) {
  const m = useDeprecateArtifact(projectId);
  return (
    <Dialog open onClose={() => !m.isPending && onClose()} titleId="deprecate-title">
      <DialogHeader titleId="deprecate-title">Deprecate {artifactLabel(artifact)}?</DialogHeader>
      <p className="mb-3 text-sm text-text-tertiary">
        The version is kept with its lineage and runs, but it can no longer be promoted. Deprecation can't be undone from the dashboard.
      </p>
      {m.error && <Alert variant="danger">{errorMessage(m.error, "Couldn't deprecate")}</Alert>}
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={m.isPending}>Cancel</Button>
        <Button variant="danger" loading={m.isPending} disabled={m.isPending} onClick={() => m.mutate(artifact.id, { onSuccess: onClose })}>
          Deprecate
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function blockersFrom(err: unknown): PromotionBlocker[] | null {
  // The route raises {error: 'promotion_blocked', message, blockers}; the
  // envelope keeps those keys under details (code is the status there).
  if (!(err instanceof ApiError)) return null;
  if (err.code !== 'promotion_blocked' && err.details?.error !== 'promotion_blocked') return null;
  const b = err.details?.blockers;
  return Array.isArray(b) ? (b as PromotionBlocker[]) : null;
}

function BlockerList({ blockers }: { blockers: PromotionBlocker[] }) {
  return (
    <ul className="list-disc space-y-0.5 pl-4">
      {blockers.map((b, i) => <li key={`${b.code}-${i}`}>{b.message}</li>)}
    </ul>
  );
}

/** The stage stepper, a live check of the chosen target, and Promote. */
export function PromotionPanel({ projectId, artifact, canPromote }: { projectId: string; artifact: Artifact; canPromote: boolean }) {
  const current = STAGES.indexOf(artifact.stage);
  const forward = STAGES.filter((_, i) => i > current);
  const [picked, setTarget] = useState<ArtifactStage | ''>(forward[0] ?? '');
  // After a promotion the stage moves on; fall back to the next stage.
  const target = picked && forward.includes(picked) ? picked : (forward[0] ?? '');
  const to = target || null;
  const check = usePromotionCheck(projectId, artifact.id, to);
  const promote = usePromoteArtifact(projectId);
  const [confirm, setConfirm] = useState(false);
  const deprecated = !!artifact.deprecated_at;
  const failed = blockersFrom(promote.error);
  return (
    <section aria-labelledby="promotion-heading" className="space-y-3 rounded-lg border border-border p-4" data-testid="promotion-panel">
      <h4 id="promotion-heading" className="text-sm font-semibold text-text-primary">Stage</h4>
      <ol className="flex flex-wrap items-center gap-1 text-xs" aria-label="Stages">
        {STAGES.map((s, i) => (
          <li key={s} className="flex items-center gap-1" aria-current={s === artifact.stage ? 'step' : undefined}>
            <span
              className={`rounded-full border px-2 py-0.5 ${
                s === artifact.stage
                  ? 'border-brand bg-surface-active font-semibold text-text-primary'
                  : i < current
                    ? 'border-border text-text-tertiary'
                    : 'border-border text-text-muted'
              }`}
            >
              {stageLabel(s)}
            </span>
            {i < STAGES.length - 1 && <span aria-hidden className="text-text-muted">→</span>}
          </li>
        ))}
      </ol>
      {deprecated ? (
        <p className="text-sm text-text-tertiary">Deprecated versions can't be promoted.</p>
      ) : forward.length === 0 ? (
        <p className="text-sm text-text-tertiary">This version is archived.</p>
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-2">
            <div className="w-full sm:w-48">
              <Select
                title="Promote to"
                value={target}
                onValueChange={(v) => {
                  setTarget(v as ArtifactStage);
                  promote.reset();
                }}
                options={forward.map((s) => ({ value: s, label: stageLabel(s) }))}
              />
            </div>
            {canPromote && (
              <Button
                size="sm"
                disabled={!check.data?.allowed || promote.isPending}
                disabledReason={
                  check.data && !check.data.allowed
                    ? 'Blocked by the promotion gate. The reasons are listed below.'
                    : !check.data
                      ? 'Checking the promotion gate'
                      : undefined
                }
                loading={promote.isPending}
                onClick={() => setConfirm(true)}
              >
                Promote to {to ? stageLabel(to) : '…'}
              </Button>
            )}
          </div>
          <div aria-live="polite">
            {check.isLoading && <p className="text-sm text-text-tertiary">Checking the gate…</p>}
            {check.error && <Alert variant="danger" title="Couldn't check promotion">{errorMessage(check.error)}</Alert>}
            {check.data && (check.data.allowed ? (
              <Alert variant="success" announce={false} title={`Ready for ${stageLabel(check.data.to_stage)}`}>
                {check.data.to_stage === 'staging' || check.data.to_stage === 'production'
                  ? 'Every required suite has a trusted pass on this exact version.'
                  : 'No eval gate applies to this stage.'}
              </Alert>
            ) : (
              <Alert variant="warning" announce={false} title={`Blocked from ${stageLabel(check.data.to_stage)}`}>
                <BlockerList blockers={check.data.blockers} />
              </Alert>
            ))}
            {!canPromote && <p className="mt-2 text-xs text-text-tertiary">Promoting needs project edit access on a Team plan.</p>}
          </div>
          {promote.error && (
            <Alert variant="danger" title="Promotion refused">
              {failed ? <BlockerList blockers={failed} /> : errorMessage(promote.error)}
            </Alert>
          )}
        </>
      )}
      {confirm && to && (
        <Dialog open onClose={() => !promote.isPending && setConfirm(false)} titleId="promote-title">
          <DialogHeader titleId="promote-title">Promote {artifactLabel(artifact)} to {stageLabel(to)}?</DialogHeader>
          <p className="mb-3 text-sm text-text-tertiary">
            {to === 'production'
              ? 'The family’s current production version moves to Archived in the same step.'
              : 'The server checks the gate again before it moves the version.'}
          </p>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirm(false)} disabled={promote.isPending}>Cancel</Button>
            <Button
              loading={promote.isPending}
              disabled={promote.isPending}
              onClick={() => promote.mutate({ artifactId: artifact.id, toStage: to }, { onSettled: () => setConfirm(false) })}
            >
              Promote
            </Button>
          </DialogFooter>
        </Dialog>
      )}
    </section>
  );
}

function FamilyView({
  projectId,
  kind,
  name,
  allowed,
  canAdmin,
  onBack,
  onOpen,
  onOpenRun,
}: Props & {
  kind: ArtifactKind;
  name: string;
  allowed: boolean;
  onBack: () => void;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="space-y-5" data-testid="artifact-family">
      <Button variant="ghost" size="sm" onClick={onBack}>
        ← All artifacts
      </Button>
      <div className="min-w-0">
        <h3 className="break-all text-md font-semibold text-text-primary">{name}</h3>
        <p className="text-xs text-text-tertiary">{KIND_LABEL[kind] ?? kind} family</p>
      </div>
      <FamilyMatrix projectId={projectId} kind={kind} name={name} onOpen={onOpen} onOpenRun={onOpenRun} />
      <RequirementsEditor projectId={projectId} kind={kind} name={name} editable={canAdmin && allowed} />
    </div>
  );
}

function FamilyMatrix({
  projectId,
  kind,
  name,
  onOpen,
  onOpenRun,
}: {
  projectId: string;
  kind: ArtifactKind;
  name: string;
  onOpen: (id: string) => void;
  onOpenRun: (id: string) => void;
}) {
  const { data, isLoading, error } = useFamilyEvals(projectId, kind, name);
  if (isLoading) return <p className="text-sm text-text-tertiary">Loading versions × suites…</p>;
  if (error || !data) return <Alert variant="danger" title="Couldn't load the matrix">{errorMessage(error)}</Alert>;
  type Row = (typeof data.rows)[number];
  return (
    <section aria-labelledby="matrix-heading" className="space-y-2">
      <h4 id="matrix-heading" className="text-sm font-semibold text-text-primary">Versions × suites</h4>
      <p className="text-xs text-text-tertiary">Each cell is the latest trusted run of that suite on that version.</p>
      <Table<Row>
        label="Versions by suite"
        minWidth={Math.max(480, 200 + data.columns.length * 160)}
        rowKey={(r) => r.artifact_id}
        data={data.rows}
        emptyState="No versions yet."
        columns={[
          {
            key: 'version',
            header: 'Version',
            render: (r) => (
              <span className="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => onOpen(r.artifact_id)}
                  className="break-all text-left font-mono text-xs text-text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                >
                  {r.version}
                </button>
                <StageBadge stage={r.stage} deprecated={r.deprecated} />
              </span>
            ),
          },
          ...data.columns.map((c) => ({
            key: c.suite_lineage_id,
            header: (
              <span>
                {c.name ?? c.slug ?? c.suite_lineage_id}
                {c.required && <span className="ml-1 normal-case tracking-normal text-warning">required</span>}
              </span>
            ),
            render: (r: Row) => {
              const cell = r.cells[c.suite_lineage_id];
              if (!cell) return <span className="text-xs text-text-muted">No trusted run</span>;
              return (
                <button
                  type="button"
                  onClick={() => onOpenRun(cell.run_id)}
                  aria-label={`${c.name ?? c.slug ?? 'Suite'} on ${r.version}: ${cell.verdict ?? 'no verdict'}, ${pct(cell.pass_rate)}. Open run`}
                  className="inline-flex items-center gap-1.5 rounded focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                >
                  <RunVerdictBadge verdict={cell.verdict} state="finished" />
                  <span className="text-xs tabular-nums text-text-secondary">{pct(cell.pass_rate)}</span>
                </button>
              );
            },
          })),
        ]}
      />
    </section>
  );
}

interface TagRow {
  key: string;
  value: string;
}

function RequirementsEditor({ projectId, kind, name, editable }: { projectId: string; kind: ArtifactKind; name: string; editable: boolean }) {
  const req = useFamilyRequirements(projectId, kind, name);
  const suites = useEvalSuites(projectId);
  const save = useSaveFamilyRequirements(projectId);
  // Local edits; null = showing what the server has.
  const [draft, setDraft] = useState<{ suites: string[]; tags: TagRow[] } | null>(null);
  const [saved, setSaved] = useState(false);
  if (req.isLoading) return <p className="text-sm text-text-tertiary">Loading requirements…</p>;
  if (req.error || !req.data) return <Alert variant="danger" title="Couldn't load requirements">{errorMessage(req.error)}</Alert>;
  const serverTags = Object.entries(req.data.require_tags).map(([key, value]) => ({ key, value }));
  const current = draft ?? { suites: req.data.suites, tags: serverTags };
  const known = suites.data?.suites ?? [];
  const nameOf = (lineage: string) => known.find((s) => s.lineage_id === lineage)?.name ?? lineage;
  const options = [...known.map((s) => s.lineage_id), ...current.suites.filter((l) => !known.some((s) => s.lineage_id === l))];
  const edit = (next: { suites: string[]; tags: TagRow[] }) => {
    setSaved(false);
    setDraft(next);
  };
  const badTag = current.tags.some((t) => !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(t.key) || !t.value.trim());
  const submit = () => {
    const require_tags: Record<string, string> = {};
    for (const t of current.tags) require_tags[t.key] = t.value.trim();
    save.mutate(
      { kind, name, suites: current.suites, require_tags },
      { onSuccess: () => { setDraft(null); setSaved(true); } },
    );
  };
  return (
    <section aria-labelledby="requirements-heading" className="space-y-3 rounded-lg border border-border p-4" data-testid="family-requirements">
      <div>
        <h4 id="requirements-heading" className="text-sm font-semibold text-text-primary">Promotion requirements</h4>
        <p className="text-xs text-text-tertiary">
          Staging and production need a trusted pass of every required suite on the exact version. Set by project admins.
        </p>
      </div>
      {!editable ? (
        <div className="space-y-2 text-sm">
          {req.data.suites.length === 0 ? (
            <p className="text-text-tertiary">No suites required: any version can be promoted.</p>
          ) : (
            <ul className="list-disc pl-5 text-text-secondary">{req.data.suites.map((l) => <li key={l}>{nameOf(l)}</li>)}</ul>
          )}
          {serverTags.length > 0 && (
            <p className="text-text-secondary">
              Training data must carry {serverTags.map((t) => `${t.key}=${t.value}`).join(', ')}.
            </p>
          )}
        </div>
      ) : (
        <>
          <fieldset className="space-y-1.5">
            <legend className="text-sm font-medium text-text-secondary">Required suites</legend>
            {options.length === 0 && <p className="text-sm text-text-tertiary">No suites in this project yet.</p>}
            {options.map((l) => (
              <Checkbox
                key={l}
                label={nameOf(l)}
                checked={current.suites.includes(l)}
                onChange={(e) =>
                  edit({ ...current, suites: e.target.checked ? [...current.suites, l] : current.suites.filter((x) => x !== l) })
                }
              />
            ))}
          </fieldset>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-text-secondary">Tags the training data must carry</legend>
            {current.tags.map((t, i) => (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label={`Tag ${i + 1} key`}
                  placeholder="phi_compliant"
                  value={t.key}
                  className="w-40"
                  onChange={(e) => edit({ ...current, tags: current.tags.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)) })}
                />
                <Input
                  aria-label={`Tag ${i + 1} value`}
                  placeholder="v3"
                  value={t.value}
                  className="w-32"
                  onChange={(e) => edit({ ...current, tags: current.tags.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)) })}
                />
                <Button variant="ghost" size="sm" onClick={() => edit({ ...current, tags: current.tags.filter((_, j) => j !== i) })}>
                  Remove<span className="sr-only"> tag {i + 1}</span>
                </Button>
              </div>
            ))}
            <Button variant="secondary" size="sm" onClick={() => edit({ ...current, tags: [...current.tags, { key: '', value: '' }] })}>
              Add tag
            </Button>
            {badTag && <p className="text-xs text-danger">Tag keys are lowercase slugs and every tag needs a value.</p>}
          </fieldset>
          {save.error && <Alert variant="danger" title="Couldn't save">{errorMessage(save.error)}</Alert>}
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={submit} disabled={!draft || badTag || save.isPending} loading={save.isPending}>
              Save requirements
            </Button>
            {draft && (
              <Button variant="ghost" size="sm" onClick={() => setDraft(null)} disabled={save.isPending}>
                Discard changes
              </Button>
            )}
            <span aria-live="polite" className="text-xs text-text-tertiary">{saved ? 'Saved.' : ''}</span>
          </div>
        </>
      )}
    </section>
  );
}
