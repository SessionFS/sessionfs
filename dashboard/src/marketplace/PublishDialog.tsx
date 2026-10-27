import { useId, useState } from 'react';
import type { MarketplacePackage, PublishTarget } from '../api/client';
import { Button, Dialog, DialogFooter, Input, Select } from '../components/ui';
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
import { CommandWell } from './CommandWell';
import { VISIBILITY_LABELS } from './capabilities';
import { ChevronIcon } from './icons';

const CLI_PUBLISH = 'sfs skill publish <dir>';

export function CliPackingNote() {
  return (
    <div className="text-xs leading-5 text-text-tertiary">
      <p>
        Have a folder instead? Packing a folder is CLI-only: it builds the archive and runs the same checks as the
        server first.
      </p>
      <div className="mt-2">
        <CommandWell command={CLI_PUBLISH} label="CLI publish command" />
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
  const [detailsOpen, setDetailsOpen] = useState(false);

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
    <Dialog open onClose={onClose} titleId="publish-package-title" className="!max-w-2xl max-h-[90vh] overflow-y-auto">
      <h2 id="publish-package-title" className="text-lg font-semibold text-text-primary">Publish a package</h2>
      <p className="mt-1 mb-5 text-sm text-text-secondary">Upload a packed skill or plugin. It is scanned and checked before it is listed.</p>

      {published ? (
        <div className="space-y-4" aria-live="polite">
          <div>
            <p className="text-sm font-semibold text-accent">Published {published.display_name || published.name}</p>
            <dl className="mt-3 divide-y divide-border border-y border-border text-xs">
              <div className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4"><dt className="text-text-tertiary sm:w-28">Version</dt><dd className="font-mono text-text-primary">{published.version || 'unversioned'} · {published.version_id}</dd></div>
              <div className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4"><dt className="text-text-tertiary sm:w-28">Listing</dt><dd className="font-mono text-text-primary">{published.collection} / {published.namespace}</dd></div>
              <div className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4"><dt className="text-text-tertiary sm:w-28">Visibility</dt><dd className="text-text-primary">{VISIBILITY_LABELS[published.visibility] ?? published.visibility} · {published.verification_state}</dd></div>
              <div className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4"><dt className="text-text-tertiary sm:w-28">Content hash</dt><dd className="min-w-0 break-all font-mono text-text-primary">{published.content_hash}</dd></div>
            </dl>
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
          <div className="flex flex-col gap-1">
            <label htmlFor={fileInputId} className="text-sm font-medium text-text-secondary">Package archive (.tar.gz)</label>
            <input
              id={fileInputId}
              type="file"
              accept=".tar.gz,.tgz,application/gzip,application/x-gzip"
              onChange={chooseFile}
              className="block w-full rounded-lg border border-dashed border-border-strong bg-bg-sunken p-3 text-sm text-text-secondary file:mr-3 file:rounded-md file:border file:border-border file:bg-surface file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-text-primary"
            />
            {file && <p className="text-xs text-text-tertiary">{file.name} · {formatBytes(file.size)}</p>}
          </div>
          <Select
            title="Publish to"
            value={selected}
            onValueChange={(value) => { setTarget(value); setProblem(null); }}
            options={targetOptions}
            hint={
              [
                !personalAllowed && 'Your personal library needs the Starter plan or above; you can still publish into organizations you administer.',
                isOrg && 'Organization and public listings must declare a license in plugin.json.',
              ].filter(Boolean).join(' ') || undefined
            }
          />
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
          <details
            className="group"
            open={detailsOpen || !!namespaceError || !!collectionError}
            onToggle={(event) => setDetailsOpen((event.currentTarget as HTMLDetailsElement).open)}
          >
            <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded text-sm font-medium text-text-secondary hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] [&::-webkit-details-marker]:hidden">
              <ChevronIcon size={14} className="transition-transform group-open:rotate-90" />
              Namespace and collection
              <span className="font-normal text-text-tertiary">(optional)</span>
            </summary>
            <div className="mt-3 grid gap-4 sm:grid-cols-2">
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
            <p className="mt-2 text-xs text-text-tertiary">Leave blank to use the default ({serverDefault}).</p>
          </details>
          <div className="border-t border-border pt-4">
            <CliPackingNote />
          </div>
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
