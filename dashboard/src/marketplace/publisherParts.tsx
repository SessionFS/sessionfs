import type { MarketplacePackage } from '../api/client';
import type { GithubOidcProvenance, GitlabOidcProvenance } from '../api/publisherTypes';
import { Badge } from '../components/Badge';
import { VerifiedIcon } from './icons';
import { isVerifiedPublisher } from './capabilities';

const REPO_RE = /^github\.com\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;
// host[:port][/root]/group[/subgroup...]/project, as the server writes it.
const GITLAB_REPO_RE = /^[a-z0-9.-]+(?::[0-9]{1,5})?(?:\/[A-Za-z0-9_][A-Za-z0-9._-]{0,254}){2,22}$/;
const SHA_RE = /^[0-9a-f]{40}$/;
const DIGITS_RE = /^[0-9]{1,20}$/;

/**
 * Publisher identity for a listing. The badge text is the verification
 * method and subject the server derived from a live verification row
 * ("verified: acme.com", "verified: github.com/acme (org #123)"); a display
 * name never earns a badge on its own.
 */
export function PublisherBadge({ item, compact = false }: { item: MarketplacePackage; compact?: boolean }) {
  const publisher = item.publisher;
  if (!publisher) return null;
  if (publisher.status !== 'active') {
    return <Badge label={`publisher ${publisher.status}`} variant="danger" tint={compact} />;
  }
  if (!publisher.verified || publisher.badges.length === 0) {
    return compact ? null : <Badge label="unverified publisher" variant="default" />;
  }
  const label = compact ? 'verified publisher' : publisher.badges[0];
  return (
    <span title={publisher.badges.join('\n')} className="inline-flex">
      <Badge label={label} variant="success" tint={compact} />
    </span>
  );
}

/**
 * Compact publisher mark for package cards: a decorative check icon next to
 * the namespace (the card's title button carries the accessible "verified
 * publisher" text, and the dossier shows the badge itself), a danger badge
 * for a suspended or revoked publisher, nothing for an unverified one.
 */
export function VerifiedMark({ item }: { item: MarketplacePackage }) {
  const publisher = item.publisher;
  if (!publisher) return null;
  if (publisher.status !== 'active') return <Badge label={`publisher ${publisher.status}`} variant="danger" tint />;
  if (!isVerifiedPublisher(item)) return null;
  return (
    <span className="inline-flex text-brand" aria-hidden="true" data-testid="verified-mark">
      <VerifiedIcon size={14} />
    </span>
  );
}


/** "pending review" / "rejected" listing state (visible to the publisher). */
export function ListingReviewBadge({ item }: { item: MarketplacePackage }) {
  if (item.moderation_state === 'pending_review') return <Badge label="pending review" variant="warning" tint />;
  if (item.moderation_state === 'rejected') return <Badge label="rejected" variant="danger" tint />;
  return null;
}

function isProvenance(value: unknown): value is GithubOidcProvenance {
  return !!value && typeof value === 'object' && (value as { kind?: unknown }).kind === 'github_oidc';
}

/**
 * "Published from": which GitHub Actions run published a version through
 * trusted publishing. This is publication provenance from GitHub's verified
 * OIDC token, NOT build provenance: nothing binds the archive to the commit.
 * Links are constructed only from validated fields (repository shape, 40-hex
 * SHA, numeric run id), never from a server-supplied URL.
 */
function isGitlabProvenance(value: unknown): value is GitlabOidcProvenance {
  return !!value && typeof value === 'object' && (value as { kind?: unknown }).kind === 'gitlab_oidc';
}

function GitlabProvenancePanel({ signature }: { signature: GitlabOidcProvenance }) {
  const repo = signature.published_from_repo && GITLAB_REPO_RE.test(signature.published_from_repo) ? signature.published_from_repo : null;
  const commit = signature.publishing_run_sha && SHA_RE.test(signature.publishing_run_sha) ? signature.publishing_run_sha : null;
  const jobId = signature.publishing_job_id && DIGITS_RE.test(signature.publishing_job_id) ? signature.publishing_job_id : null;
  const repoUrl = repo ? `https://${repo}` : null;
  const rows: [string, React.ReactNode][] = [
    ['Project', repoUrl ? <a href={repoUrl} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">{repo}</a> : 'unknown'],
    ['Project id', signature.published_from_project_id ?? 'unknown'],
    ['Run commit', repoUrl && commit
      ? <a href={`${repoUrl}/-/commit/${commit}`} target="_blank" rel="noopener noreferrer" className="font-mono text-brand hover:underline">{commit.slice(0, 12)}</a>
      : 'unknown'],
    ['Pipeline source', signature.publishing_event || 'unknown'],
    ['Ref', <span className="font-mono">{signature.publishing_ref || 'unknown'}{signature.publishing_ref_protected ? ' (protected)' : ''}</span>],
    ['Environment', signature.publishing_environment ? `${signature.publishing_environment}${signature.publishing_environment_protected ? ' (protected)' : ' (not protected)'}` : 'none'],
    ['Publishing job', repoUrl && jobId
      ? <a href={`${repoUrl}/-/jobs/${jobId}`} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">#{jobId}</a>
      : 'unknown'],
    ['Runner', signature.runner_environment || 'unknown'],
  ];
  return (
    <section className="mt-6" aria-labelledby="provenance-heading">
      <h3 id="provenance-heading" className="text-sm font-semibold text-text-primary">Published from</h3>
      <p className="mt-1 text-xs text-text-secondary">
        Published by a trusted GitLab CI job; SessionFS verified GitLab&apos;s signed ID token for it.
        This shows where the version was published from, not how it was built: no build attestation binds this
        archive to the commit yet.
      </p>
      <dl className="mt-3 grid gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label} className="flex gap-2">
            <dt className="w-28 shrink-0 text-text-tertiary">{label}</dt>
            <dd className="break-all text-text-primary">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export function ProvenancePanel({ item }: { item: MarketplacePackage }) {
  const signature = item.signature;
  if (isGitlabProvenance(signature)) {
    return <GitlabProvenancePanel signature={signature} />;
  }
  if (!isProvenance(signature)) {
    return (
      <section className="mt-6" aria-labelledby="provenance-heading">
        <h3 id="provenance-heading" className="text-sm font-semibold text-text-primary">Published from</h3>
        <p className="mt-1 text-xs text-text-secondary">
          Uploaded with an API key. No CI publishing run is recorded for this version.
        </p>
      </section>
    );
  }
  const repo = signature.published_from_repo && REPO_RE.test(signature.published_from_repo) ? signature.published_from_repo : null;
  const commit = signature.publishing_run_sha && SHA_RE.test(signature.publishing_run_sha) ? signature.publishing_run_sha : null;
  const runId = signature.publishing_run_id && DIGITS_RE.test(signature.publishing_run_id) ? signature.publishing_run_id : null;
  const repoUrl = repo ? `https://${repo}` : null;
  const rows: [string, React.ReactNode][] = [
    ['Repository', repoUrl ? <a href={repoUrl} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">{repo}</a> : 'unknown'],
    ['Repository id', signature.published_from_repo_id ?? 'unknown'],
    ['Run commit', repoUrl && commit
      ? <a href={`${repoUrl}/commit/${commit}`} target="_blank" rel="noopener noreferrer" className="font-mono text-brand hover:underline">{commit.slice(0, 12)}</a>
      : 'unknown'],
    ['Workflow', <span className="font-mono">{signature.publishing_workflow}</span>],
    ['Event', signature.publishing_event || 'unknown'],
    ['Ref', <span className="font-mono">{signature.publishing_ref || 'unknown'}</span>],
    ['Environment', signature.publishing_environment || 'none'],
    ['Publishing run', repoUrl && runId
      ? <a href={`${repoUrl}/actions/runs/${runId}`} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">#{runId}</a>
      : 'unknown'],
    ['Runner', signature.runner_environment || 'unknown'],
  ];
  return (
    <section className="mt-6" aria-labelledby="provenance-heading">
      <h3 id="provenance-heading" className="text-sm font-semibold text-text-primary">Published from</h3>
      <p className="mt-1 text-xs text-text-secondary">
        Published by a trusted GitHub Actions workflow run; SessionFS verified GitHub&apos;s signed OIDC token for it.
        This shows where the version was published from, not how it was built: no build attestation binds this
        archive to the commit yet.
      </p>
      <dl className="mt-3 grid gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label} className="flex gap-2">
            <dt className="w-28 shrink-0 text-text-tertiary">{label}</dt>
            <dd className="break-all text-text-primary">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
