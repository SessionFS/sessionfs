import { Alert, Badge, MonoText } from '../../components/ui';
import type {
  Artifact,
  ArtifactStage,
  EvalResultVerdict,
  EvalRun,
  EvalRunState,
  EvalRunVerdict,
} from '../../api/client';
import { safeArtifactLink, stageLabel } from './evalUtils';

const VERDICT_VARIANT: Record<EvalRunVerdict, 'success' | 'danger' | 'warning'> = {
  passed: 'success',
  failed: 'danger',
  incomplete: 'warning',
};

export function RunVerdictBadge({ verdict, state }: { verdict: EvalRunVerdict | null; state?: EvalRunState }) {
  if (state === 'voided') return <Badge variant="default" tint label="Voided" />;
  if (state === 'expired') return <Badge variant="default" tint label="Expired" />;
  if (state === 'running' || !verdict) return <Badge variant="info" tint label="Running" />;
  return <Badge variant={VERDICT_VARIANT[verdict]} tint label={stageLabel(verdict)} />;
}

const RESULT_VARIANT: Record<EvalResultVerdict, 'success' | 'danger' | 'warning' | 'default'> = {
  pass: 'success',
  fail: 'danger',
  error: 'warning',
  skip: 'default',
};

export function ResultVerdictBadge({ verdict }: { verdict: EvalResultVerdict }) {
  return <Badge variant={RESULT_VARIANT[verdict]} tint label={stageLabel(verdict)} />;
}

/**
 * Trusted or Unverified, straight from the server's trust fields. A run is
 * only ever "Trusted" when the server says so; an unfinished run has no label.
 */
export function TrustBadge({ run }: { run: Pick<EvalRun, 'trusted' | 'trust_label' | 'untrusted_reason' | 'state'> }) {
  if (run.state === 'voided') return null;
  if (run.trust_label === 'trusted' && run.trusted) return <Badge variant="success" label="Trusted" />;
  if (run.trust_label === 'unverified' || (run.trust_label === 'trusted' && !run.trusted)) {
    return (
      <span className="inline-flex min-w-0 items-center gap-1.5" title={run.untrusted_reason ?? undefined}>
        <Badge variant="warning" label="Unverified" />
        {run.untrusted_reason && <span className="sr-only">: {run.untrusted_reason}</span>}
      </span>
    );
  }
  return null;
}

export function StageBadge({ stage, deprecated }: { stage: ArtifactStage; deprecated?: boolean }) {
  const variant = stage === 'production' ? 'success' : stage === 'staging' ? 'info' : 'default';
  return (
    <span className="inline-flex items-center gap-1">
      <Badge variant={variant} tint label={stageLabel(stage)} />
      {deprecated && <Badge variant="warning" tint label="Deprecated" />}
    </span>
  );
}

export function ArtifactUri({ artifact }: { artifact: Pick<Artifact, 'external_uri' | 'link_url'> }) {
  const link = safeArtifactLink(artifact);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <MonoText value={artifact.external_uri} tailChars={24} copyable className="min-w-0 text-xs" />
      {link ? (
        <a
          href={link}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 text-xs font-medium text-brand underline-offset-2 hover:underline focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          Open<span className="sr-only"> {artifact.external_uri} in a new tab</span>
        </a>
      ) : (
        <span className="shrink-0 text-2xs text-text-muted">Copy only</span>
      )}
    </div>
  );
}

export function PlanNotice({ allowed, what }: { allowed: boolean; what: string }) {
  if (allowed) return null;
  return (
    <Alert variant="info" title="Evaluations are a Team feature" announce={false}>
      Records already here stay readable. {what} need Team or Enterprise on this project's plan.
    </Alert>
  );
}
