import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  useProjects,
  useMergeProject,
} from '../hooks/useProjects';
import type {
  MergeDryRunResponse,
  MergeExecuteResponse,
  MergeCollision,
} from '../api/client';
import { ApiError } from '../api/client';
import {
  Button,
  Card,
  Select,
} from '../components/ui';

function isDryRun(r: MergeDryRunResponse | MergeExecuteResponse): r is MergeDryRunResponse {
  return r.dry_run === true;
}

function isExecute(r: MergeDryRunResponse | MergeExecuteResponse): r is MergeExecuteResponse {
  return r.dry_run === false;
}

function CollisionList({
  title,
  collisions,
  emptyLabel,
}: {
  title: string;
  collisions: MergeCollision[];
  emptyLabel: string;
}) {
  if (!collisions.length) {
    return (
      <div className="text-xs text-text-tertiary">
        {emptyLabel}
      </div>
    );
  }
  return (
    <div>
      <span className="text-xs font-medium text-text-secondary">{title}:</span>
      <ul className="mt-1 space-y-1">
        {collisions.map((c, i) => (
          <li key={i} className="text-xs text-text-secondary ml-4 list-disc">
            {c.old_name && c.new_name ? (
              <>
                <span className="font-mono text-text-primary">{c.old_name}</span>
                {' '}&rarr;{' '}
                <span className="font-mono text-[var(--warning)]">{c.new_name}</span>
                {c.display_note && (
                  <span className="text-text-tertiary ml-1">({c.display_note})</span>
                )}
              </>
            ) : c.old_slug && c.new_slug ? (
              <>
                <span className="font-mono text-text-primary">{c.old_slug}</span>
                {' '}&rarr;{' '}
                <span className="font-mono text-[var(--warning)]">{c.new_slug}</span>
              </>
            ) : (
              <span className="font-mono text-text-primary">{JSON.stringify(c)}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function DryRunPlan({ result }: { result: MergeDryRunResponse }) {
  const { stats } = result;
  return (
    <div className="space-y-4">
      {/* Summary header */}
      <div className="rounded-lg border border-[var(--brand)] bg-[rgba(26,115,232,0.06)] px-4 py-3">
        <p className="text-sm font-semibold text-text-primary mb-1">Dry-run plan</p>
        <p className="text-xs text-text-secondary">
          {stats.personas_reassigned + stats.tickets_reassigned + stats.knowledge_entries_reassigned + stats.wiki_pages_reassigned}{' '}
          items to reassign
          {result.persona_collisions.length + result.slug_collisions.length > 0 && (
            <span className="text-[var(--warning)]">
              {', '}{result.persona_collisions.length + result.slug_collisions.length} collision(s)
            </span>
          )}
        </p>
      </div>

      {/* Per-table plan */}
      <div className="space-y-3">
        {/* Personas */}
        <div className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
          <p className="text-sm font-medium text-text-primary mb-1">
            Personas ({stats.personas_total} total, {stats.personas_reassigned} to reassign
            {result.persona_collisions.length > 0 && (
              <span className="text-[var(--warning)]">, {result.persona_collisions.length} collision(s)</span>
            )}
            )
          </p>
          <CollisionList
            title=""
            collisions={result.persona_collisions}
            emptyLabel="All unique — will be reassigned"
          />
        </div>

        {/* Tickets */}
        <div className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
          <p className="text-sm font-medium text-text-primary mb-1">
            Tickets ({stats.tickets_total} total, {stats.tickets_reassigned} to reassign)
          </p>
          <p className="text-xs text-text-tertiary">
            Ticket IDs are globally unique — no collisions possible
          </p>
        </div>

        {/* Knowledge entries */}
        <div className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
          <p className="text-sm font-medium text-text-primary mb-1">
            Knowledge entries ({stats.knowledge_entries_total} total, {stats.knowledge_entries_reassigned} to reassign
            {stats.knowledge_entries_skipped > 0 && (
              <span className="text-text-tertiary">, {stats.knowledge_entries_skipped} skipped (duplicates)</span>
            )}
            )
          </p>
          <p className="text-xs text-text-tertiary">
            Exact duplicates are skipped — no data loss
          </p>
        </div>

        {/* Wiki pages */}
        <div className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
          <p className="text-sm font-medium text-text-primary mb-1">
            Wiki pages ({stats.wiki_pages_total} total, {stats.wiki_pages_reassigned} to reassign
            {result.slug_collisions.length > 0 && (
              <span className="text-[var(--warning)]">, {result.slug_collisions.length} collision(s)</span>
            )}
            )
          </p>
          <CollisionList
            title=""
            collisions={result.slug_collisions}
            emptyLabel="All unique — will be reassigned"
          />
        </div>

        {/* Rules */}
        <div className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
          <p className="text-sm font-medium text-text-primary mb-1">
            Rules
          </p>
          <p className="text-xs text-text-secondary">
            {stats.rules_action === 'none'
              ? 'No rules to reassign'
              : stats.rules_action === 'promoted'
                ? 'Source rules will be promoted (target has none)'
                : stats.rules_action === 'archived'
                  ? 'Source rules will be archived as a wiki page snapshot (target has rules)'
                  : `Rules action: ${stats.rules_action || 'unknown'}`}
          </p>
        </div>
      </div>

      {/* Warning */}
      <div className="rounded-lg border border-[rgba(239,68,68,0.2)] bg-[rgba(239,68,68,0.04)] px-4 py-3">
        <p className="text-xs font-medium text-[var(--danger)] mb-1">
          This is a one-way operation
        </p>
        <p className="text-xs text-text-tertiary">
          The source project will be soft-deleted. Its sessions remain accessible but
          it will no longer appear in the project list. This cannot be automatically undone.
        </p>
      </div>
    </div>
  );
}

function ExecuteOutcome({ result }: { result: MergeExecuteResponse }) {
  const isSuccess = result.status === 'completed';
  return (
    <div
      className={`rounded-lg border px-4 py-3 ${
        isSuccess
          ? 'border-emerald-500/30 bg-emerald-500/5'
          : 'border-[rgba(239,68,68,0.3)] bg-[rgba(239,68,68,0.05)]'
      }`}
    >
      <div className="flex items-center gap-2 mb-2">
        {isSuccess ? (
          <svg className="w-5 h-5 text-emerald-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <polyline points="22 4 12 14.01 9 11.01" />
          </svg>
        ) : (
          <svg className="w-5 h-5 text-[var(--danger)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="15" y1="9" x2="9" y2="15" />
            <line x1="9" y1="9" x2="15" y2="15" />
          </svg>
        )}
        <span className={`text-sm font-semibold ${isSuccess ? 'text-emerald-500' : 'text-[var(--danger)]'}`}>
          Merge {isSuccess ? 'completed' : 'failed'}
        </span>
      </div>
      {result.error_message && (
        <p className="text-xs text-[var(--danger)] mb-2">{result.error_message}</p>
      )}
      <p className="text-xs text-text-tertiary">
        Audit ID: <code className="font-mono">{result.audit_id}</code>
      </p>
      <div className="mt-2 space-y-1">
        {result.persona_renames.length > 0 && (
          <p className="text-xs text-text-secondary">
            {result.persona_renames.length} persona(s) renamed
          </p>
        )}
        {result.slug_renames.length > 0 && (
          <p className="text-xs text-text-secondary">
            {result.slug_renames.length} wiki slug(s) renamed
          </p>
        )}
        {result.skipped_ke_ids.length > 0 && (
          <p className="text-xs text-text-secondary">
            {result.skipped_ke_ids.length} duplicate knowledge entries skipped
          </p>
        )}
      </div>
    </div>
  );
}

interface MergeSurfaceProps {
  projectId: string;
  projectName: string;
}

export default function MergeSurface({ projectId, projectName }: MergeSurfaceProps) {
  const navigate = useNavigate();
  const { data: allProjects, isLoading: loadingProjects } = useProjects();
  const mergeProject = useMergeProject(projectId);

  type Step = 'select' | 'review' | 'confirm' | 'result';
  const [step, setStep] = useState<Step>('select');
  const [sourceId, setSourceId] = useState('');
  const [dryRunResult, setDryRunResult] = useState<MergeDryRunResponse | null>(null);
  const [executeResult, setExecuteResult] = useState<MergeExecuteResponse | null>(null);
  const [error, setError] = useState('');
  const [confirmChecked, setConfirmChecked] = useState(false);

  // Other projects (not this one, not merged/tombstoned)
  const otherProjects = (allProjects || []).filter(
    (p) => p.id !== projectId && !p.merged_into_project_id,
  );

  function handleDryRun() {
    if (!sourceId) {
      setError('Select a source project.');
      return;
    }
    setError('');
    setDryRunResult(null);
    setExecuteResult(null);
    mergeProject.mutate(
      { source_project_id: sourceId, dry_run: true },
      {
        onSuccess: (result) => {
          if (isDryRun(result)) {
            setDryRunResult(result);
            setStep('review');
          }
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            setError(err.message);
          } else {
            setError(String(err));
          }
        },
      },
    );
  }

  function handleExecute() {
    if (!sourceId) return;
    setError('');
    setExecuteResult(null);
    mergeProject.mutate(
      { source_project_id: sourceId, dry_run: false },
      {
        onSuccess: (result) => {
          if (isExecute(result)) {
            setExecuteResult(result);
            setStep('result');
          }
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            setError(err.message);
          } else {
            setError(String(err));
          }
        },
      },
    );
  }

  function handleReset() {
    setStep('select');
    setSourceId('');
    setDryRunResult(null);
    setExecuteResult(null);
    setError('');
    setConfirmChecked(false);
  }

  if (loadingProjects) {
    return <p className="p-5 text-text-tertiary text-sm">Loading projects…</p>;
  }

  return (
    <div className="p-5">
      <div className="mb-4">
        <h2 className="text-base font-semibold text-text-primary mb-1">Merge into this project</h2>
        <p className="text-sm text-text-tertiary">
          Fold another project&apos;s personas, tickets, knowledge entries, wiki pages,
          and repos into <span className="font-medium text-text-secondary">{projectName}</span>.
          The source project will be soft-deleted.
        </p>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
          {error}
        </div>
      )}

      {step === 'select' && (
        <Card level="elevated" className="p-4">
          <div className="mb-4">
            <label className="block text-sm font-medium text-text-secondary mb-2">
              Source project to merge in:
            </label>
            {otherProjects.length === 0 ? (
              <p className="text-text-tertiary text-sm">
                No other projects available to merge.
              </p>
            ) : (
              <Select
                aria-label="Source project"
                value={sourceId}
                onValueChange={(v) => { setSourceId(v); setError(''); }}
                options={otherProjects.map((p) => ({
                  value: p.id,
                  label: p.name || p.git_remote_normalized,
                }))}
                placeholder="Select a project…"
              />
            )}
          </div>
          <Button
            onClick={handleDryRun}
            disabled={!sourceId || otherProjects.length === 0 || mergeProject.isPending}
            loading={mergeProject.isPending}
          >
            Preview merge (dry-run)
          </Button>
          <p className="text-xs text-text-tertiary mt-3">
            Dry-run performs all validation and reports what WOULD happen without
            writing anything. No side effects.
          </p>
        </Card>
      )}

      {step === 'review' && dryRunResult && (
        <div>
          <DryRunPlan result={dryRunResult} />

          <div className="mt-4 flex items-center gap-3">
            <Button variant="ghost" onClick={handleReset}>
              Back
            </Button>
            <Button
              variant="primary"
              onClick={() => setStep('confirm')}
            >
              Continue to execute
            </Button>
          </div>
        </div>
      )}

      {step === 'confirm' && (
        <Card level="elevated" className="p-4 mt-4">
          <p className="text-sm font-medium text-text-primary mb-3">
            Confirm merge execution
          </p>
          <p className="text-sm text-text-secondary mb-3">
            This will permanently reassign all data from the source project into{' '}
            <span className="font-medium text-text-primary">{projectName}</span>.
            The source project will be soft-deleted. This cannot be automatically undone.
          </p>
          <label className="flex items-center gap-2 text-sm text-text-secondary mb-4">
            <input
              type="checkbox"
              checked={confirmChecked}
              onChange={(e) => setConfirmChecked(e.target.checked)}
              className="accent-[var(--brand)]"
            />
            I understand — execute the merge
          </label>
          <div className="flex items-center gap-3">
            <Button variant="ghost" onClick={() => setStep('review')}>
              Back
            </Button>
            <Button
              variant="danger"
              onClick={handleExecute}
              disabled={!confirmChecked || mergeProject.isPending}
              loading={mergeProject.isPending}
            >
              Execute merge
            </Button>
          </div>
        </Card>
      )}

      {step === 'result' && executeResult && (
        <div>
          <ExecuteOutcome result={executeResult} />

          <div className="mt-4 flex items-center gap-3">
            {executeResult.status === 'completed' && (
              <Button onClick={() => navigate('/projects')}>
                Back to projects
              </Button>
            )}
            <Button variant="ghost" onClick={handleReset}>
              Merge another
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
