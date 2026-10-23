import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { PublisherDetail, PublisherVerification, StartVerificationInput } from '../api/publisherTypes';
import { Badge } from '../components/Badge';
import CopyButton from '../components/CopyButton';
import EmptyState from '../components/EmptyState';
import { Button, Card, Input, Select } from '../components/ui';
import { useMe } from '../hooks/useMe';
import MarketplaceNav from './MarketplaceNav';
import {
  useAddTrustedPublisher,
  useCheckVerification,
  useClaimPublisher,
  useMyPublishers,
  usePublisherListings,
  useRemoveTrustedPublisher,
  useRevokeVerification,
  useStartVerification,
} from '../hooks/usePublishers';
import { useToast } from '../hooks/useToast';
import { useMyOrgs } from '../transfers/useTransfers';
import { adminOrgs } from './moderationState';
import { workflowSnippet } from './publisherWorkflow';
import { errorMessage } from '../utils/errorMessage';

const NAMESPACE_RE = /^[a-z0-9](?:[a-z0-9-]{1,37})[a-z0-9]$/;

const STATUS_VARIANT: Record<PublisherVerification['status'], 'success' | 'warning' | 'danger' | 'default'> = {
  verified: 'success',
  pending: 'warning',
  lapsed: 'danger',
  revoked: 'default',
};

function ClaimForm() {
  const me = useMe();
  const orgs = useMyOrgs();
  const claim = useClaimPublisher();
  const { addToast } = useToast();
  const [namespace, setNamespace] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [owner, setOwner] = useState('');
  const admin = adminOrgs(orgs.data, me.data);
  const valid = NAMESPACE_RE.test(namespace) && !namespace.includes('--');

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    try {
      const created = await claim.mutateAsync({
        namespace,
        ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
        ...(owner ? { org_id: owner } : {}),
      });
      addToast('success', `Claimed ${created.namespace}. Verify it next.`);
      setNamespace('');
      setDisplayName('');
    } catch (error) {
      addToast('error', `Claim failed: ${errorMessage(error)}`);
    }
  }

  return (
    <Card className="p-5">
      <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_1fr_200px_auto] sm:items-end" aria-label="Claim a namespace">
        <Input
          title="Namespace"
          value={namespace}
          onChange={(event) => setNamespace(event.target.value.toLowerCase())}
          placeholder="acme"
          maxLength={39}
          error={namespace && !valid ? '3-39 lowercase letters, digits or single hyphens' : undefined}
        />
        <Input title="Display name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={200} placeholder="Acme Inc." />
        <Select
          title="Owner"
          value={owner}
          onValueChange={setOwner}
          options={[{ value: '', label: 'Personal' }, ...admin.map((org) => ({ value: org.org_id, label: org.name }))]}
        />
        <Button type="submit" disabled={!valid} loading={claim.isPending}>Claim</Button>
      </form>
      <p className="mt-3 text-xs leading-5 text-text-tertiary">
        Namespaces are global and never released. Names that imitate a reserved brand or a verified publisher
        (lookalike letters, digits for letters, added "-official" and similar) are refused.
      </p>
    </Card>
  );
}

function VerificationRow({ publisher, row }: { publisher: PublisherDetail; row: PublisherVerification }) {
  const check = useCheckVerification();
  const revoke = useRevokeVerification();
  const { addToast } = useToast();
  async function run(kind: 'check' | 'revoke') {
    try {
      const mutation = kind === 'check' ? check : revoke;
      const result = await mutation.mutateAsync({ publisherId: publisher.id, verificationId: row.id });
      addToast(result.status === 'verified' ? 'success' : 'info', `${row.subject_display}: ${result.status}`);
    } catch (error) {
      addToast('error', `${kind === 'check' ? 'Check' : 'Revoke'} failed: ${errorMessage(error)}`);
    }
  }
  return (
    <li className="rounded-lg border border-border bg-bg-sunken p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-text-primary">{row.subject_display}</span>
          <Badge label={row.method} />
          <Badge label={row.status} variant={STATUS_VARIANT[row.status]} tint />
        </div>
        <div className="flex gap-2">
          {row.status !== 'revoked' && (
            <Button size="sm" variant="secondary" onClick={() => run('check')} loading={check.isPending}>Check now</Button>
          )}
          {row.status !== 'revoked' && (
            <Button size="sm" variant="ghost" onClick={() => run('revoke')} loading={revoke.isPending}>Remove</Button>
          )}
        </div>
      </div>
      {row.status === 'pending' && row.txt_record && (
        <div className="mt-2 text-xs text-text-secondary">
          Add this TXT record at <span className="font-mono">{row.txt_host}</span>, then check:
          <div className="mt-1 flex items-center gap-2">
            <code className="rounded bg-bg-elevated px-2 py-1 font-mono text-text-primary">{row.txt_record}</code>
            <CopyButton text={row.txt_record} label="Copy TXT record" />
          </div>
        </div>
      )}
      {row.status === 'lapsed' && (
        <p className="mt-2 text-xs text-danger">The DNS record has been missing for 7 days; the badge is off until it is back.</p>
      )}
      {row.status_reason && row.status !== 'verified' && (
        <p className="mt-1 font-mono text-2xs text-text-tertiary">{row.status_reason}</p>
      )}
      {row.last_checked_at && <p className="mt-1 text-2xs text-text-tertiary">Last checked {new Date(row.last_checked_at).toLocaleString()}</p>}
    </li>
  );
}

function AddVerification({ publisher }: { publisher: PublisherDetail }) {
  const start = useStartVerification();
  const { addToast } = useToast();
  const [method, setMethod] = useState<StartVerificationInput['method']>('dns_txt');
  const [value, setValue] = useState('');
  const options = [
    { value: 'dns_txt', label: 'DNS TXT record' },
    ...(publisher.owner_type === 'org' ? [{ value: 'github_org', label: 'GitHub organization' }] : []),
    { value: 'github_user', label: 'GitHub account' },
  ];

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const body: StartVerificationInput = method === 'dns_txt'
      ? { method, domain: value.trim() }
      : { method, installation_id: Number(value) };
    try {
      const row = await start.mutateAsync({ publisherId: publisher.id, body });
      addToast(row.status === 'verified' ? 'success' : 'info', row.status === 'verified' ? `Verified ${row.subject_display}` : 'Add the TXT record shown, then check.');
      setValue('');
    } catch (error) {
      addToast('error', `Verification failed: ${errorMessage(error)}`);
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 grid gap-2 sm:grid-cols-[200px_1fr_auto] sm:items-end" aria-label={`Verify ${publisher.namespace}`}>
      <Select title="Method" value={method} onValueChange={(next) => { setMethod(next as StartVerificationInput['method']); setValue(''); }} options={options} />
      <Input
        title={method === 'dns_txt' ? 'Domain' : 'GitHub App installation id'}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={method === 'dns_txt' ? 'acme.com' : '12345678'}
        inputMode={method === 'dns_txt' ? undefined : 'numeric'}
      />
      <Button type="submit" variant="secondary" disabled={!value.trim() || (method !== 'dns_txt' && !/^[0-9]+$/.test(value))} loading={start.isPending}>
        Verify
      </Button>
      {method !== 'dns_txt' && (
        <p className="text-2xs text-text-tertiary sm:col-span-3">
          Install the SessionFS GitHub App on the account, link it in Settings, then enter its installation id.
          Only your own installations count; the badge is bound to GitHub&apos;s numeric account id.
        </p>
      )}
    </form>
  );
}

function TrustedPublishers({ publisher }: { publisher: PublisherDetail }) {
  const add = useAddTrustedPublisher();
  const remove = useRemoveTrustedPublisher();
  const { addToast } = useToast();
  const [repository, setRepository] = useState('');
  const [workflow, setWorkflow] = useState('release.yml');
  const [environment, setEnvironment] = useState('release');
  const [refPattern, setRefPattern] = useState('refs/tags/v*');
  const [packageName, setPackageName] = useState('');
  const [reusable, setReusable] = useState('');

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    try {
      await add.mutateAsync({
        publisherId: publisher.id,
        body: {
          repository: repository.trim(),
          workflow: workflow.trim(),
          ...(environment.trim() ? { environment: environment.trim() } : {}),
          ...(refPattern.trim() ? { ref_pattern: refPattern.trim() } : {}),
          ...(packageName.trim() ? { package_name: packageName.trim() } : {}),
          ...(reusable.trim() ? { reusable_workflow: reusable.trim() } : {}),
        },
      });
      addToast('success', `Trusted ${repository.trim()}`);
      setRepository('');
    } catch (error) {
      addToast('error', `Add trusted publisher failed: ${errorMessage(error)}`);
    }
  }

  async function removeOne(trustedId: string) {
    try {
      await remove.mutateAsync({ publisherId: publisher.id, trustedId });
      addToast('success', 'Trusted publisher removed.');
    } catch (error) {
      addToast('error', `Remove failed: ${errorMessage(error)}`);
    }
  }

  const rows = publisher.trusted_publishers || [];
  return (
    <section className="mt-5" aria-labelledby={`trusted-${publisher.id}`}>
      <h3 id={`trusted-${publisher.id}`} className="text-sm font-semibold text-text-primary">Trusted publishing (GitHub Actions)</h3>
      <p className="mt-1 text-xs text-text-secondary">
        A workflow listed here publishes with a short-lived OIDC token instead of an API key. The trust is bound to the
        repository&apos;s numeric id, so a renamed or recreated repository does not inherit it.
      </p>
      {rows.length > 0 && (
        <ul className="mt-2 space-y-2">
          {rows.map((row) => (
            <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-bg-sunken p-3 text-xs">
              <span className="font-mono text-text-primary">
                {row.provider === 'gitlab'
                  ? `GitLab ${row.gitlab_host ?? ''} · ${row.repository} (project #${row.gitlab_project_id})`
                  : `${row.repository} (#${row.repository_id}) · ${row.workflow}`}
                {row.reusable_workflow ? ` → ${row.reusable_workflow}` : ''}
                {row.environment ? ` · env ${row.environment}` : ''}
                {row.require_protected_environment ? ' (must be protected)' : ''}
                {row.ref_pattern ? ` · ${row.ref_pattern}` : ''}
                {row.package_name ? ` · ${row.package_name}` : ''}
              </span>
              {row.provider === 'gitlab' && !row.gitlab_namespace_id && (
                <span title="If this GitLab project is transferred to another namespace, pipelines there still match this trust. Re-create the trust with a pinned namespace id.">
                  <Badge label="Namespace not pinned" variant="warning" tint />
                </span>
              )}
              <Button size="sm" variant="ghost" onClick={() => removeOne(row.id)} loading={remove.isPending}>Remove</Button>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={submit} className="mt-3 grid gap-2 sm:grid-cols-2" aria-label={`Add trusted publisher for ${publisher.namespace}`}>
        <Input title="Repository (owner/repo)" value={repository} onChange={(event) => setRepository(event.target.value)} placeholder="acme/skills" />
        <Input title="Workflow file" value={workflow} onChange={(event) => setWorkflow(event.target.value)} placeholder="release.yml" />
        <Input title="Environment (recommended)" value={environment} onChange={(event) => setEnvironment(event.target.value)} />
        <Input title="Ref pattern" value={refPattern} onChange={(event) => setRefPattern(event.target.value)} />
        <Input title="Package (optional)" value={packageName} onChange={(event) => setPackageName(event.target.value)} placeholder="any package in the namespace" />
        <Input title="Reusable workflow (optional)" value={reusable} onChange={(event) => setReusable(event.target.value)} placeholder="owner/repo/.github/workflows/publish.yml" />
        <div className="flex items-end">
          <Button type="submit" variant="secondary" disabled={!/^[^/\s]+\/[^/\s]+$/.test(repository.trim()) || !workflow.trim()} loading={add.isPending}>
            Add trusted publisher
          </Button>
        </div>
      </form>
      <details className="mt-3 text-xs text-text-secondary">
        <summary className="cursor-pointer text-text-primary">Example workflow</summary>
        <div className="mt-2 flex items-start gap-2">
          <pre className="flex-1 overflow-x-auto rounded-lg bg-bg-sunken p-3 font-mono text-2xs">{workflowSnippet(publisher.namespace)}</pre>
          <CopyButton text={workflowSnippet(publisher.namespace)} label="Copy workflow" />
        </div>
      </details>
    </section>
  );
}

function Listings({ publisher }: { publisher: PublisherDetail }) {
  const listings = usePublisherListings(publisher.id);
  const items = listings.data?.items || [];
  if (items.length === 0) return null;
  return (
    <section className="mt-5" aria-labelledby={`listings-${publisher.id}`}>
      <h3 id={`listings-${publisher.id}`} className="text-sm font-semibold text-text-primary">Listings</h3>
      <ul className="mt-2 space-y-1 text-xs">
        {items.map((item) => (
          <li key={item.listing_id} className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-text-primary">{item.name} {item.version || ''}</span>
            <Badge label={item.visibility} tint />
            <Badge
              label={(item.moderation_state || 'approved').replace('_', ' ')}
              variant={item.moderation_state === 'pending_review' ? 'warning' : item.moderation_state === 'rejected' ? 'danger' : 'success'}
              tint
            />
            {item.moderation_reason && <span className="font-mono text-2xs text-text-tertiary">{item.moderation_reason}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function PublisherCard({ publisher }: { publisher: PublisherDetail }) {
  const verifications = publisher.verifications || [];
  return (
    <Card className="p-5" aria-label={`Publisher ${publisher.namespace}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-2xs font-semibold uppercase tracking-[0.15em] text-text-tertiary">{publisher.owner_type} publisher</p>
          <h2 className="mt-1 font-mono text-lg font-semibold text-text-primary">{publisher.namespace}</h2>
          <p className="text-sm text-text-secondary">{publisher.display_name}</p>
        </div>
        <div className="flex flex-wrap gap-1">
          <Link
            to={`/marketplace/publishers/${encodeURIComponent(publisher.id)}/analytics`}
            className="rounded-lg border border-border px-2 py-0.5 text-xs text-text-secondary hover:border-border-strong hover:text-text-primary"
          >
            Analytics
          </Link>
          {publisher.verified ? publisher.badges.map((badge) => <Badge key={badge} label={badge} variant="success" />) : <Badge label="not verified" />}
          {publisher.status !== 'active' && <Badge label={publisher.status} variant="danger" />}
          <Badge label={publisher.public_publishing ? 'public publishing on' : 'public publishing off'} variant={publisher.public_publishing ? 'info' : 'default'} tint />
        </div>
      </div>
      {publisher.suspended_reason && <p className="mt-2 text-xs text-danger">Suspended: {publisher.suspended_reason}</p>}
      {(publisher.review_flags || []).length > 0 && (
        <p className="mt-2 text-xs text-warning">Similar to {publisher.review_flags!.map((flag) => flag.conflict).join(', ')}; a moderator may review this namespace.</p>
      )}
      <section className="mt-4" aria-labelledby={`verify-${publisher.id}`}>
        <h3 id={`verify-${publisher.id}`} className="text-sm font-semibold text-text-primary">Verification</h3>
        {verifications.length > 0 && (
          <ul className="mt-2 space-y-2">
            {verifications.map((row) => <VerificationRow key={row.id} publisher={publisher} row={row} />)}
          </ul>
        )}
        {publisher.status === 'active' && <AddVerification publisher={publisher} />}
      </section>
      {publisher.status === 'active' && <TrustedPublishers publisher={publisher} />}
      <Listings publisher={publisher} />
    </Card>
  );
}

export default function PublishersPage() {
  const publishers = useMyPublishers();
  const items = publishers.data?.items || [];
  return (
    <div className="mx-auto w-full max-w-5xl space-y-5 px-4 py-7 sm:px-6">
      <MarketplaceNav />
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-text-primary">Publishers</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-text-secondary">
          Claim a namespace, prove who you are with a DNS record or a GitHub account, and let a GitHub Actions
          workflow publish without a stored API key. Verified publishers can be invited to publish public packages;
          the first public version and any update that asks for more access are reviewed by SessionFS.
        </p>
      </header>
      <ClaimForm />
      {publishers.isLoading && <p className="text-sm text-text-tertiary">Loading publishers…</p>}
      {publishers.error && <p role="alert" className="text-sm text-danger">Could not load publishers: {errorMessage(publishers.error)}</p>}
      {!publishers.isLoading && !publishers.error && items.length === 0 && (
        <EmptyState title="No publishers yet" description="Claim a namespace above to start." />
      )}
      {items.map((publisher) => <PublisherCard key={publisher.id} publisher={publisher} />)}
    </div>
  );
}
