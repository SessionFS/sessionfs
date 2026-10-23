import { useId, useState } from 'react';
import type { MarketplacePackage, PublishTarget } from '../api/client';
import { Badge } from '../components/Badge';
import CopyButton from '../components/CopyButton';
import { Button, Dialog, DialogFooter, DialogHeader, Input, Select } from '../components/ui';
import { usePublishSkillPackage } from '../hooks/useMarketplace';
import { useMe } from '../hooks/useMe';
import { useToast } from '../hooks/useToast';
import { useMyOrgs } from '../transfers/useTransfers';
import { formatBytes } from '../utils/format';
import {
  COLLECTION_HELP,
  COLLECTION_RE,
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_MIB,
  NAMESPACE_HELP,
  NAMESPACE_RE,
  REASON_HINTS,
  explainPublishError,
  type Explanation,
} from './marketplaceErrors';
import { adminOrgs, canPublishPersonal, isPlatformAdmin } from './moderationState';
import { ExplanationAlert } from './parts';

const CLI_PUBLISH = 'sfs skill publish <dir>';

export function CliPackingNote() {
  return (
    <div className="rounded-lg border border-border bg-bg-sunken p-3 text-xs leading-5 text-text-secondary">
      <p>
        The dashboard uploads a pre-built <span className="font-mono">.tar.gz</span>. Packing a folder is CLI-only:
        it builds a deterministic archive and runs the same checks as the server first.
      </p>
      <div className="mt-2 flex items-center gap-2">
        <code className="rounded bg-bg-elevated px-2 py-1 font-mono text-text-primary">{CLI_PUBLISH}</code>
        <CopyButton text={CLI_PUBLISH} label="Copy command" />
      </div>
    </div>
  );
}

function localFileProblem(file: File): Explanation | null {
  const name = file.name.toLowerCase();
  if (!name.endsWith('.tar.gz') && !name.endsWith('.tgz')) {
    return { title: 'Choose a .tar.gz (or .tgz) archive.', fix: REASON_HINTS.invalid_archive, code: 'invalid_archive' };
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return {
      title: `The archive is ${formatBytes(file.size)}; uploads are limited to ${MAX_UPLOAD_MIB} MiB compressed.`,
      fix: REASON_HINTS.package_too_large,
      code: 'package_too_large',
    };
  }
  return null;
}

export default function PublishDialog({ onClose }: { onClose: () => void }) {
  const me = useMe();
  const orgs = useMyOrgs();
  const publish = usePublishSkillPackage();
  const { addToast } = useToast();
  const fileInputId = useId();

  const personalAllowed = canPublishPersonal(me.data);
  const orgChoices = adminOrgs(orgs.data, me.data);
  const platformAdmin = isPlatformAdmin(me.data);
  const targetOptions = [
    ...(personalAllowed ? [{ value: 'personal', label: 'My personal library' }] : []),
    ...orgChoices.map((org) => ({ value: `org:${org.org_id}`, label: `Organization: ${org.name}` })),
  ];

  const [target, setTarget] = useState<string>('');
  const [visibility, setVisibility] = useState<'org' | 'public'>('org');
  const [file, setFile] = useState<File | null>(null);
  const [namespace, setNamespace] = useState('');
  const [collection, setCollection] = useState('');
  const [problem, setProblem] = useState<Explanation | null>(null);
  const [published, setPublished] = useState<MarketplacePackage | null>(null);

  const selected = target || targetOptions[0]?.value || '';
  const isOrg = selected.startsWith('org:');
  const org = isOrg ? orgChoices.find((entry) => `org:${entry.org_id}` === selected) : undefined;
  const serverDefault = isOrg ? 'org' : 'personal';
  const normalizedNamespace = namespace.trim().toLowerCase();
  const normalizedCollection = collection.trim().toLowerCase();
  const namespaceError = normalizedNamespace && !NAMESPACE_RE.test(normalizedNamespace) ? `Namespace must be ${NAMESPACE_HELP}` : undefined;
  const collectionError = normalizedCollection && !COLLECTION_RE.test(normalizedCollection) ? `Collection must be ${COLLECTION_HELP}` : undefined;

  function chooseFile(event: React.ChangeEvent<HTMLInputElement>) {
    const next = event.target.files?.[0] ?? null;
    setFile(next);
    setProblem(next ? localFileProblem(next) : null);
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!file || !selected) return;
    const local = localFileProblem(file);
    if (local) {
      setProblem(local);
      return;
    }
    const publishTarget: PublishTarget = isOrg && org
      ? { kind: 'org', orgId: org.org_id, visibility: platformAdmin ? visibility : 'org' }
      : { kind: 'personal' };
    setProblem(null);
    try {
      const result = await publish.mutateAsync({
        target: publishTarget,
        file,
        filename: file.name,
        namespace: normalizedNamespace || undefined,
        collection: normalizedCollection || undefined,
      });
      setPublished(result);
      addToast('success', `Published ${result.display_name || result.name} ${result.version || ''}`.trim());
    } catch (error) {
      setProblem(explainPublishError(error, {
        kind: isOrg ? 'org' : 'personal',
        orgName: org?.name,
        collection: normalizedCollection || serverDefault,
      }));
    }
  }

  const loading = me.isLoading || orgs.isLoading;

  return (
    <Dialog open onClose={onClose} titleId="publish-package-title" className="max-w-2xl max-h-[90vh] overflow-y-auto">
      <p className="text-2xs font-semibold uppercase tracking-[0.16em] text-brand">Publish to the marketplace</p>
      <DialogHeader titleId="publish-package-title">Publish a skill package</DialogHeader>

      {published ? (
        <div className="space-y-4" aria-live="polite">
          <div className="rounded-lg border border-accent/40 bg-success/6 p-4">
            <p className="text-sm font-semibold text-accent">Published {published.display_name || published.name}</p>
            <dl className="mt-3 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
              <div><dt className="text-text-tertiary">Version</dt><dd className="font-mono text-text-primary">{published.version || 'unversioned'} · {published.version_id}</dd></div>
              <div><dt className="text-text-tertiary">Listing</dt><dd className="font-mono text-text-primary">{published.collection} / {published.namespace}</dd></div>
              <div className="sm:col-span-2"><dt className="text-text-tertiary">Content hash</dt><dd className="break-all font-mono text-text-primary">{published.content_hash}</dd></div>
            </dl>
            <div className="mt-3 flex gap-2"><Badge label={published.visibility} tint /><Badge label={published.verification_state} tint /></div>
          </div>
          <DialogFooter>
            <Button onClick={onClose}>Done</Button>
          </DialogFooter>
        </div>
      ) : loading ? (
        <p className="text-sm text-text-tertiary">Checking where you can publish…</p>
      ) : targetOptions.length === 0 ? (
        <div className="space-y-4">
          <ExplanationAlert
            tone="warning"
            explanation={{
              title: 'Publishing needs the Starter plan or above, or an owner/admin role in an organization with the team skill library.',
              fix: 'Upgrade your plan, or ask an organization owner to publish the package.',
              code: 'upgrade_required',
            }}
          />
          <CliPackingNote />
          <DialogFooter><Button variant="ghost" onClick={onClose}>Close</Button></DialogFooter>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-4">
          <CliPackingNote />
          <div className="flex flex-col gap-1">
            <label htmlFor={fileInputId} className="text-sm font-medium text-text-secondary">Package archive (.tar.gz)</label>
            <input
              id={fileInputId}
              type="file"
              accept=".tar.gz,.tgz,application/gzip,application/x-gzip"
              onChange={chooseFile}
              className="block w-full text-sm text-text-secondary file:mr-3 file:rounded-lg file:border file:border-border file:bg-surface file:px-3 file:py-1.5 file:text-xs file:text-text-primary"
            />
            {file && <p className="text-xs text-text-tertiary">{file.name} · {formatBytes(file.size)}</p>}
          </div>
          <Select title="Publish to" value={selected} onValueChange={(value) => { setTarget(value); setProblem(null); }} options={targetOptions} />
          {!personalAllowed && (
            <p className="text-xs text-text-tertiary">Your personal library needs the Starter plan or above; you can still publish into organizations you administer.</p>
          )}
          {isOrg && platformAdmin && (
            <Select
              title="Visibility"
              value={visibility}
              onValueChange={(value) => setVisibility(value as 'org' | 'public')}
              options={[
                { value: 'org', label: 'Organization members only' },
                { value: 'public', label: 'Public marketplace (platform admin)' },
              ]}
            />
          )}
          {isOrg && (
            <p className="text-xs text-text-tertiary">Organization and public listings must declare a license in plugin.json.</p>
          )}
          <div className="grid gap-4 sm:grid-cols-2">
            <Input
              title="Namespace"
              placeholder={serverDefault}
              value={namespace}
              onChange={(event) => setNamespace(event.target.value)}
              error={namespaceError}
              maxLength={100}
            />
            <Input
              title="Collection"
              placeholder={serverDefault}
              value={collection}
              onChange={(event) => setCollection(event.target.value)}
              error={collectionError}
              maxLength={100}
            />
          </div>
          <p className="-mt-2 text-xs text-text-tertiary">Leave blank to use the server default ({serverDefault}).</p>
          {problem && <ExplanationAlert explanation={problem} />}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button
              type="submit"
              loading={publish.isPending}
              disabled={!file || !selected || !!namespaceError || !!collectionError || publish.isPending}
            >
              Publish
            </Button>
          </DialogFooter>
        </form>
      )}
    </Dialog>
  );
}
