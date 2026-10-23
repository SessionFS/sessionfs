import { useDeferredValue, useState } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { isTierGate } from './tierGate';
import type { MarketplaceCategory, MarketplacePackage } from '../api/client';
import { Badge } from '../components/Badge';
import EmptyState from '../components/EmptyState';
import { Button, Card, Dialog, DialogFooter, DialogHeader, Input, Select, TableShell, Tabs, TabPanel } from '../components/ui';
import {
  MARKETPLACE_TOOLS,
  type MarketplaceFilterState,
  useCreateSkillInstallRequest,
  useDeletePersonalSkillVersion,
  useEffectiveSkillAssignments,
  useMarketplaceCategories,
  useMarketplacePackages,
  useMarketplacePreview,
  useMarketplaceReadme,
  useOrgSkillControl,
  useRevocationIndex,
} from '../hooks/useMarketplace';
import { DeprecationNotice, FreshnessBadges, ToolFreshnessTable } from './discoveryParts';
import { installsLabel } from './discoveryUtils';
import { SafeReadme } from './SafeReadme';
import { useMarketplaceDisclosure } from '../hooks/useMarketplaceDisclosure';
import { useMe } from '../hooks/useMe';
import MarketplaceNav from './MarketplaceNav';
import { useProjects } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { useMyOrgs } from '../transfers/useTransfers';
import { formatBytes } from '../utils/format';
import { STATE_COPY, adminOrgs, canPublishPersonal, listingState, type ModerationState } from './moderationState';
import { ModerationStateBadge, RevocationIncompleteNotice } from './parts';
import { DisclosureCard } from './DisclosureCard';
import { UseInYourTool, WorksInMatrix } from './NativeCatalogs';
import { HeldUpdateQueues } from './HeldUpdates';
import { RegistryList } from './RegistryList';
import PublishDialog from './PublishDialog';
import { ListingReviewBadge, ProvenancePanel, PublisherBadge } from './publisherParts';
import ReportDialog from './ReportDialog';
import { errorMessage } from '../utils/errorMessage';

const TOOL_OPTIONS = [
  { value: '', label: 'All tools' },
  ...MARKETPLACE_TOOLS.map((tool) => ({ value: tool, label: tool })),
];

export { isTierGate } from './tierGate';

function shortHash(value: string) {
  return value.length > 24 ? `${value.slice(0, 19)}…` : value;
}

function PackageCard({ item, state, onOpen }: { item: MarketplacePackage; state: ModerationState; onOpen: () => void }) {
  const riskCount = [item.risk.scripts, item.risk.mcp, item.risk.executable_extensions, item.risk.hooks, (item.risk.allowed_tools || []).length > 0].filter(Boolean).length;
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group w-full text-left rounded-xl border border-border bg-bg-elevated p-4 transition-[border-color,transform,box-shadow] hover:-translate-y-0.5 hover:border-border-strong hover:shadow-[var(--shadow-md)] focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      aria-label={`Inspect ${item.display_name}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-2xs font-semibold uppercase tracking-[0.15em] text-text-tertiary">
            {item.collection} / {item.namespace}
          </p>
          <h2 className="mt-1 truncate text-base font-semibold text-text-primary group-hover:text-brand">
            {item.display_name}
          </h2>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">
          <PublisherBadge item={item} compact />
          <ListingReviewBadge item={item} />
          <ModerationStateBadge state={state} />
          <FreshnessBadges item={item} />
        </div>
      </div>
      <p className="mt-3 line-clamp-2 min-h-10 text-sm leading-5 text-text-secondary">
        {item.description || 'No package description provided.'}
      </p>
      {(item.categories || []).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {(item.categories || []).map((slug) => <Badge key={slug} label={slug} tint />)}
        </div>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
        <span className="font-mono">{item.version || 'unversioned'}</span>
        <span aria-hidden="true">·</span>
        <span>{Object.keys(item.compatibility).length} targets</span>
        <span aria-hidden="true">·</span>
        <span title="Installs in the last 30 days">{installsLabel(item.installs?.total)} installs</span>
        <span aria-hidden="true">·</span>
        <span className={riskCount ? 'text-warning' : 'text-accent'}>
          {riskCount ? `${riskCount} elevated capability ${riskCount === 1 ? 'flag' : 'flags'}` : 'instruction-only'}
        </span>
        <Badge label={item.visibility} variant={item.visibility === 'public' ? 'info' : 'default'} tint />
      </div>
    </button>
  );
}

function DeclaredBehavior({ item }: { item: MarketplacePackage }) {
  const declared = Object.entries(item.risk.disclosed_metadata || {}).filter(([, fields]) => Object.keys(fields || {}).length > 0);
  if (declared.length === 0) return null;
  return (
    <section className="mt-4 rounded-lg border border-border bg-bg-sunken px-3 py-2" aria-labelledby="declared-behavior-heading">
      <h3 id="declared-behavior-heading" className="text-2xs uppercase tracking-wider text-text-tertiary">Declared behavior</h3>
      <p className="mt-1 text-xs text-text-secondary">Frontmatter the target tool acts on when the skill runs (context, agent, model, effort, shell). Disclosed, not consent-gated.</p>
      <dl className="mt-2 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
        {declared.flatMap(([skill, fields]) => Object.entries(fields).map(([key, value]) => (
          <div key={`${skill}:${key}`} className="flex gap-2">
            <dt className="font-mono text-text-tertiary">{skill} · {key}</dt>
            <dd className="break-all font-mono text-text-primary">{value}</dd>
          </div>
        )))}
      </dl>
    </section>
  );
}

function TrustDossier({ item }: { item: MarketplacePackage }) {
  const allowedTools = item.risk.allowed_tools || [];
  const hookEvents = item.risk.hook_events || [];
  const entries = [
    ['Scripts', item.risk.scripts, ''],
    ['MCP servers', item.risk.mcp, ''],
    ['Executable extensions', item.risk.executable_extensions, ''],
    ['Frontmatter hooks', item.risk.hooks, hookEvents.join(', ')],
    ['Pre-approved tools', allowedTools.length > 0, allowedTools.join(', ')],
  ] as const;
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {entries.map(([label, present, detail]) => (
        <div key={label} className="rounded-lg border border-border bg-bg-sunken px-3 py-2">
          <p className="text-2xs uppercase tracking-wider text-text-tertiary">{label}</p>
          <p className={`mt-1 text-sm font-semibold ${present ? 'text-warning' : 'text-accent'}`}>
            {present ? 'Present — explicit consent' : 'Not detected'}
          </p>
          {present && detail && <p className="mt-1 break-all font-mono text-xs text-text-secondary">{detail}</p>}
        </div>
      ))}
    </div>
  );
}

/** The install disclosure card for the version (marketplace v2 §3.3). */
function PackageDisclosure({ versionId, enabled }: { versionId: string; enabled: boolean }) {
  const disclosure = useMarketplaceDisclosure(versionId, [], 'project', enabled);
  if (!enabled) return null;
  if (disclosure.isLoading) return <p className="mt-4 text-sm text-text-tertiary">Loading install disclosure…</p>;
  if (disclosure.error) return <p role="alert" className="mt-4 text-sm text-warning">Install disclosure unavailable: {errorMessage(disclosure.error)}</p>;
  if (!disclosure.data) return null;
  return <div className="mt-4"><DisclosureCard disclosure={disclosure.data} /></div>;
}

function ReadmeTab({ item, enabled }: { item: MarketplacePackage; enabled: boolean }) {
  const readme = useMarketplaceReadme(item.package_id, item.version_id, enabled);
  if (readme.isLoading) return <p className="text-sm text-text-tertiary">Loading README…</p>;
  if (readme.error) return <p role="alert" className="text-sm text-warning">README unavailable: {errorMessage(readme.error)}</p>;
  if (!readme.data) return null;
  return <SafeReadme readme={readme.data} versionId={item.version_id} />;
}

function PackageDialog({ item, state, onClose }: { item: MarketplacePackage; state: ModerationState; onClose: () => void }) {
  const [tab, setTab] = useState<'overview' | 'readme'>('overview');
  const [showPreview, setShowPreview] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const deliverable = state === 'active';
  const [requestOpen, setRequestOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const preview = useMarketplacePreview(item.version_id, showPreview);
  const { auth } = useAuth();
  const { addToast } = useToast();
  const me = useMe();
  const deleteVersion = useDeletePersonalSkillVersion();
  const ownsVersion = item.visibility === 'user' && !!item.owner_user_id && item.owner_user_id === me.data?.user_id;

  async function removeVersion() {
    try {
      await deleteVersion.mutateAsync({ packageId: item.package_id, versionId: item.version_id });
      addToast('success', `Deleted immutable version ${item.version || item.version_id}.`);
      onClose();
    } catch (error) {
      addToast('error', `Delete failed: ${errorMessage(error)}`);
    } finally {
      setConfirmDelete(false);
    }
  }

  async function download() {
    if (!deliverable) return;
    setDownloading(true);
    try {
      const blob = await auth!.client.downloadMarketplaceVersion(item.version_id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${item.name}-${item.version || item.version_id}.tar.gz`;
      anchor.click();
      URL.revokeObjectURL(url);
      addToast('success', `Downloaded immutable version ${item.version || item.version_id}`);
    } catch (error) {
      addToast('error', `Download failed: ${errorMessage(error)}`);
    } finally {
      setDownloading(false);
    }
  }

  return (
    <>
      <Dialog open onClose={onClose} titleId="marketplace-package-title" className="max-w-4xl max-h-[90vh] overflow-y-auto">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-2xs font-semibold uppercase tracking-[0.16em] text-brand">Package trust dossier</p>
            <DialogHeader titleId="marketplace-package-title">{item.display_name}</DialogHeader>
          </div>
          <div className="flex gap-2">
            <PublisherBadge item={item} />
            <ListingReviewBadge item={item} />
            <ModerationStateBadge state={state} tint={false} />
            <Badge label={item.visibility} />
          </div>
        </div>
        <p className="-mt-2 mb-5 max-w-2xl text-sm leading-6 text-text-secondary">{item.description}</p>
        {state !== 'active' && (
          <div role="status" className={`mb-5 rounded-lg border p-3 text-sm ${state === 'blocked' ? 'border-danger/40 bg-danger/6 text-danger' : 'border-warning/40 bg-warning/6 text-warning'}`}>
            <p className="font-semibold">This version is {STATE_COPY[state].label}.</p>
            <p className="mt-1 text-xs text-text-secondary">{STATE_COPY[state].help}</p>
          </div>
        )}

        <DeprecationNotice item={item} />
        <div className="mb-4">
          <Tabs
            bare
            idPrefix="mp-item"
            aria-label="Item details"
            tabs={[{ key: 'overview', label: 'Overview' }, { key: 'readme', label: 'README' }]}
            activeKey={tab}
            onChange={(key) => setTab(key as 'overview' | 'readme')}
          />
        </div>
        <TabPanel idPrefix="mp-item" tabKey={tab}>
        {tab === 'readme' && (deliverable
          ? <ReadmeTab item={item} enabled={deliverable} />
          : <p className="text-sm text-text-tertiary">The README is shown only for deliverable versions.</p>)}
        {tab === 'overview' && (<>
        <TrustDossier item={item} />
        <DeclaredBehavior item={item} />
        <ProvenancePanel item={item} />
        <PackageDisclosure versionId={item.version_id} enabled={deliverable} />
        <WorksInMatrix versionId={item.version_id} enabled={deliverable} />

        <dl className="mt-5 grid gap-x-6 gap-y-3 rounded-xl border border-border bg-bg-sunken p-4 text-sm sm:grid-cols-2">
          <div><dt className="text-xs text-text-tertiary">Immutable version</dt><dd className="mt-1 font-mono text-text-primary">{item.version || 'unversioned'} · {item.version_id}</dd></div>
          <div><dt className="text-xs text-text-tertiary">Content hash</dt><dd className="mt-1 font-mono text-text-primary" title={item.content_hash}>{shortHash(item.content_hash)}</dd></div>
          <div><dt className="text-xs text-text-tertiary">Source</dt><dd className="mt-1 break-all text-text-primary">{item.source.type}: {item.source.locator}</dd></div>
          <div><dt className="text-xs text-text-tertiary">Commit</dt><dd className="mt-1 font-mono text-text-primary">{item.source.commit || 'not pinned by publisher'}</dd></div>
          <div><dt className="text-xs text-text-tertiary">License</dt><dd className={`mt-1 ${item.license.declared ? 'text-text-primary' : 'text-warning'}`}>{item.license.value || 'Not declared'}</dd></div>
          <div><dt className="text-xs text-text-tertiary">Canonical archive</dt><dd className="mt-1 text-text-primary">{formatBytes(item.blob_size_bytes)}</dd></div>
        </dl>

        <section className="mt-6" aria-labelledby="compatibility-heading">
          <h3 id="compatibility-heading" className="text-sm font-semibold text-text-primary">Expected compatibility (via sfs skill)</h3>
          <TableShell minWidth={560} bordered wrapperClassName="mt-2">
              <thead className="bg-bg-sunken text-xs uppercase tracking-wider text-text-tertiary">
                <tr><th className="px-3 py-2">Tool</th><th className="px-3 py-2">Instructions</th><th className="px-3 py-2">Deliberate omissions</th></tr>
              </thead>
              <tbody>
                {Object.entries(item.compatibility).map(([tool, state]) => (
                  <tr key={tool} className="border-t border-border">
                    <td className="px-3 py-2 font-mono text-text-primary">{tool}</td>
                    <td className="px-3 py-2 text-accent">{state.instruction_files} files</td>
                    <td className="px-3 py-2 text-text-secondary">{state.omitted_components.join(', ') || 'None'}</td>
                  </tr>
                ))}
              </tbody>
          </TableShell>
        </section>
        <ToolFreshnessTable item={item} />
        </>)}
        </TabPanel>

        {showPreview && (
          <section className="mt-6 rounded-xl border border-warning/30 bg-warning/5 p-4" aria-live="polite">
            <h3 className="text-sm font-semibold text-warning">Untrusted instruction preview</h3>
            <p className="mt-1 text-xs text-text-secondary">Reading a preview does not approve, install, or execute the package.</p>
            {preview.isLoading && <p className="mt-3 text-sm text-text-tertiary">Loading preview…</p>}
            {preview.error && <p role="alert" className="mt-3 text-sm text-danger">Preview failed: {errorMessage(preview.error)}</p>}
            <div className="mt-3 space-y-3">
              {preview.data?.instruction_preview?.map((entry) => (
                <div key={entry.path}>
                  <p className="mb-1 font-mono text-xs text-text-tertiary">{entry.path}</p>
                  <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-bg-sunken p-3 text-xs text-text-secondary">{entry.content}</pre>
                </div>
              ))}
            </div>
          </section>
        )}

        {ownsVersion && confirmDelete && (
          <section className="mt-6 rounded-xl border border-danger/40 bg-danger/6 p-4" role="alertdialog" aria-labelledby="delete-version-title">
            <h3 id="delete-version-title" className="text-sm font-semibold text-danger">Delete this version?</h3>
            <p className="mt-1 text-xs text-text-secondary">Removes the immutable archive and listing for {item.version || item.version_id}. Refused while an approved install request or organization assignment still references it.</p>
            <div className="mt-3 flex gap-2">
              <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>Keep it</Button>
              <Button size="sm" variant="danger" onClick={removeVersion} loading={deleteVersion.isPending}>Delete version</Button>
            </div>
          </section>
        )}
        <DialogFooter>
          {ownsVersion && !confirmDelete && (
            <Button variant="ghost" onClick={() => setConfirmDelete(true)}>Delete version…</Button>
          )}
          <Button variant="ghost" onClick={() => setReportOpen(true)}>Report…</Button>
          <Button variant="ghost" onClick={() => setShowPreview((value) => !value)} disabled={!deliverable}>{showPreview ? 'Hide preview' : 'Preview instructions'}</Button>
          <Button variant="secondary" onClick={download} loading={downloading} disabled={!deliverable}>Download archive</Button>
          <Button onClick={() => setRequestOpen(true)} disabled={!deliverable}>Request for project</Button>
        </DialogFooter>
      </Dialog>
      {requestOpen && deliverable && <InstallRequestDialog item={item} onClose={() => setRequestOpen(false)} />}
      {reportOpen && <ReportDialog item={item} onClose={() => setReportOpen(false)} />}
    </>
  );
}

function InstallRequestDialog({ item, onClose }: { item: MarketplacePackage; onClose: () => void }) {
  const projects = useProjects();
  const create = useCreateSkillInstallRequest();
  const { addToast } = useToast();
  const [projectId, setProjectId] = useState('');
  const [tool, setTool] = useState('codex');
  const [scope, setScope] = useState<'project' | 'user' | 'device'>('project');
  const [deviceId, setDeviceId] = useState('');
  const projectOptions = (projects.data || []).map((project) => ({ value: project.id, label: project.name || project.git_remote_normalized }));

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    try {
      await create.mutateAsync({
        projectId,
        version_id: item.version_id,
        content_hash: item.content_hash,
        target_scope: scope,
        ...(scope === 'device' ? { target_device_id: deviceId.trim() } : {}),
        target_tools: [tool],
        requested_capabilities: ['instructions'],
      });
      addToast('success', 'Install request submitted for approval.');
      onClose();
    } catch (error) {
      addToast('error', `Request failed: ${errorMessage(error)}`);
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="install-request-title">
      <DialogHeader titleId="install-request-title">Request {item.display_name}</DialogHeader>
      <form onSubmit={submit} className="space-y-4">
        <p className="rounded-lg border border-border bg-bg-sunken p-3 text-xs leading-5 text-text-secondary">
          This requests an exact, hash-bound instruction load. It does not write files or activate scripts, hooks, or MCP servers on any device.
        </p>
        {projects.isLoading ? <p className="text-sm text-text-tertiary">Loading projects…</p> : (
          <Select title="Project" value={projectId} onValueChange={setProjectId} options={projectOptions} placeholder="Choose a project" />
        )}
        <Select title="Target tool" value={tool} onValueChange={setTool} options={TOOL_OPTIONS.slice(1)} />
        <Select
          title="Approval scope"
          value={scope}
          onValueChange={(value) => setScope(value as typeof scope)}
          options={[
            { value: 'project', label: 'Entire project' },
            { value: 'user', label: 'My user' },
            { value: 'device', label: 'One of my devices' },
          ]}
        />
        {scope === 'device' && <Input title="Device ID" value={deviceId} onChange={(event) => setDeviceId(event.target.value)} required maxLength={100} />}
        {projectOptions.length === 0 && !projects.isLoading && (
          <p role="alert" className="text-sm text-warning">Create or join a project before requesting a marketplace skill.</p>
        )}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!projectId || (scope === 'device' && !deviceId.trim())} loading={create.isPending}>Submit request</Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

function ManagedLibrary() {
  const me = useMe();
  const org = useOrgSkillControl(me.data?.org_id);
  const effective = useEffectiveSkillAssignments();
  const myOrgs = useMyOrgs();
  // Every org the viewer owns or administers, not only the primary one.
  const managed = adminOrgs(myOrgs.data, me.data);
  if (!me.data?.org_id) {
    if (managed.length === 0) return null;
    return (
      <Card level="elevated" className="mt-8 rounded-xl p-5">
        <p className="text-2xs font-semibold uppercase tracking-[0.16em] text-brand">Managed library</p>
        <HeldUpdateQueues orgs={managed} />
      </Card>
    );
  }
  const assignments = effective.data?.items || [];
  const registries = org.registries.data?.items || [];
  const canAdminPrimary = managed.some((item) => item.org_id === me.data?.org_id);
  return (
    <Card level="elevated" className="mt-8 rounded-xl p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-2xs font-semibold uppercase tracking-[0.16em] text-brand">Managed library</p>
          <h2 className="mt-1 text-lg font-semibold text-text-primary">Organization policy reaching you</h2>
          <p className="mt-1 text-sm text-text-secondary">HTTPS registries can be polled by SessionFS: new matching tags are published to your organization through the normal publish checks. Device writes remain disabled.</p>
        </div>
        <Badge label={`${assignments.length} effective assignments`} variant={assignments.length ? 'info' : 'default'} />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <section aria-labelledby="registries-heading" className="rounded-lg border border-border bg-bg-sunken p-4">
          <h3 id="registries-heading" className="text-sm font-semibold text-text-primary">Approved registries</h3>
          {org.registries.isLoading ? <p className="mt-3 text-sm text-text-tertiary">Loading registries…</p> : isTierGate(org.registries.error) ? (
            <div className="mt-3 rounded-lg border border-warning/40 bg-warning/6 p-3" role="status">
              <p className="text-sm font-semibold text-warning">Upgrade to Team</p>
              <p className="mt-1 text-xs text-text-secondary">Organization skill registries and managed assignments are a Team-tier feature. Ask your organization owner to upgrade to enable them.</p>
            </div>
          ) : org.registries.error ? (
            <p role="alert" className="mt-3 text-sm text-danger">Registries unavailable: {errorMessage(org.registries.error)}</p>
          ) : registries.length === 0 ? (
            <p className="mt-3 text-sm text-text-tertiary">No organization registries configured.</p>
          ) : (
            <RegistryList registries={registries} orgId={me.data.org_id} canAdmin={canAdminPrimary} />
          )}
        </section>
        <section aria-labelledby="assignments-heading" className="rounded-lg border border-border bg-bg-sunken p-4">
          <h3 id="assignments-heading" className="text-sm font-semibold text-text-primary">Effective assignments</h3>
          {effective.isLoading ? <p className="mt-3 text-sm text-text-tertiary">Loading assignments…</p> : assignments.length === 0 ? (
            <p className="mt-3 text-sm text-text-tertiary">No managed packages apply to this identity.</p>
          ) : (
            <ul className="mt-3 space-y-2">{assignments.map((assignment) => (
              <li key={assignment.id} className="flex items-center justify-between gap-3 text-sm">
                <span className="font-mono text-text-secondary">{assignment.version_id}</span>
                <Badge label={`${assignment.mode} · ${assignment.target_type}`} variant={assignment.mode === 'required' ? 'warning' : 'default'} tint />
              </li>
            ))}</ul>
          )}
        </section>
      </div>
      {managed.length > 0 && <HeldUpdateQueues orgs={managed} />}
    </Card>
  );
}

function CategoryBrowse({
  categories,
  active,
  onChange,
}: {
  categories: MarketplaceCategory[];
  active: string | undefined;
  onChange: (category: string | undefined) => void;
}) {
  if (categories.length === 0) return null;
  const chip = (selected: boolean) =>
    `rounded-full border px-3 py-1 text-xs transition-colors ${
      selected ? 'border-brand bg-[var(--brand-glow)] text-text-primary' : 'border-border text-text-secondary hover:border-border-strong hover:text-text-primary'
    }`;
  return (
    <nav aria-label="Browse by category" className="mt-4 flex flex-wrap gap-2">
      <button type="button" className={chip(!active)} aria-pressed={!active} onClick={() => onChange(undefined)}>All</button>
      {categories.map((category) => (
        <button
          key={category.slug}
          type="button"
          className={chip(active === category.slug)}
          aria-pressed={active === category.slug}
          onClick={() => onChange(active === category.slug ? undefined : category.slug)}
        >
          {category.display_name}
        </button>
      ))}
    </nav>
  );
}

const SEVERITY_OPTIONS = [
  { value: '', label: 'Any scan result' },
  { value: 'none', label: 'No open findings' },
  { value: 'low', label: 'Low or better' },
  { value: 'medium', label: 'Medium or better' },
  { value: 'high', label: 'High or better' },
];
const CAPABILITY_OPTIONS = [
  { value: '', label: 'Any capability' },
  { value: 'instructions_only', label: 'Instructions only' },
  { value: 'scripts', label: 'Ships scripts' },
  { value: 'mcp', label: 'Ships MCP servers' },
  { value: 'hooks', label: 'Frontmatter hooks' },
  { value: 'executables', label: 'Executable extensions' },
  { value: 'allowed_tools', label: 'Pre-approves tools' },
];
const UPDATED_OPTIONS = [
  { value: '', label: 'Any time' },
  { value: '7', label: 'Updated in 7 days' },
  { value: '30', label: 'Updated in 30 days' },
  { value: '90', label: 'Updated in 90 days' },
  { value: '365', label: 'Updated in a year' },
];
const SORT_OPTIONS = [
  { value: 'relevance', label: 'Best match' },
  { value: 'recent', label: 'Newest' },
];

/** Discovery filters (marketplace v2 §9.2); every one is applied server-side
 *  before the page is cut. */
export function FilterRail({ filters, onChange }: { filters: MarketplaceFilterState; onChange: (next: MarketplaceFilterState) => void }) {
  const [license, setLicense] = useState(filters.license || '');
  const set = (patch: Partial<MarketplaceFilterState>) => onChange({ ...filters, ...patch });
  const toggle = (key: 'verified' | 'attested') => set({ [key]: filters[key] ? undefined : true });
  return (
    <section aria-label="Marketplace filters" className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Select aria-label="Sort" value={filters.sort || 'relevance'} onValueChange={(value) => set({ sort: value as 'recent' | 'relevance' })} options={SORT_OPTIONS} />
      <Select aria-label="Maximum scan severity" value={filters.max_severity || ''} onValueChange={(value) => set({ max_severity: (value || undefined) as MarketplaceFilterState['max_severity'] })} options={SEVERITY_OPTIONS} />
      <Select aria-label="Capability" value={filters.capability || ''} onValueChange={(value) => set({ capability: value || undefined })} options={CAPABILITY_OPTIONS} />
      <Select aria-label="Works in" value={filters.works_in || ''} onValueChange={(value) => set({ works_in: value || undefined })} options={[{ value: '', label: 'Works in: any' }, ...MARKETPLACE_TOOLS.map((tool) => ({ value: tool, label: `Works in ${tool}` }))]} />
      <Select aria-label="Updated" value={filters.updated_within ? String(filters.updated_within) : ''} onValueChange={(value) => set({ updated_within: value ? Number(value) : undefined })} options={UPDATED_OPTIONS} />
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          set({ license: license.trim() || undefined });
        }}
      >
        <Input aria-label="License" placeholder="License (e.g. MIT)" value={license} onChange={(event) => setLicense(event.target.value)} onBlur={() => set({ license: license.trim() || undefined })} maxLength={100} />
      </form>
      <div className="flex flex-wrap items-center gap-3 text-xs text-text-secondary lg:col-span-2">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={!!filters.verified} onChange={() => toggle('verified')} /> Verified publishers
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={!!filters.attested} onChange={() => toggle('attested')} /> Trusted-publishing provenance
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={filters.include_deprecated === false} onChange={() => set({ include_deprecated: filters.include_deprecated === false ? undefined : false })} /> Hide deprecated
        </label>
      </div>
    </section>
  );
}

export default function MarketplacePage() {
  const [query, setQuery] = useState('');
  const deferredQuery = useDeferredValue(query);
  const [tool, setTool] = useState('');
  const [selected, setSelected] = useState<MarketplacePackage | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [filters, setFilters] = useState<MarketplaceFilterState>({ sort: 'relevance' });
  const packages = useMarketplacePackages(deferredQuery, tool, filters);
  const categories = useMarketplaceCategories();
  const revocations = useRevocationIndex();
  const me = useMe();
  const myOrgs = useMyOrgs();
  const canPublish = canPublishPersonal(me.data) || adminOrgs(myOrgs.data, me.data).length > 0;
  const items = packages.data?.pages.flatMap((page) => page.items) ?? [];
  const loadedCount = items.length;

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-7 sm:px-6">
      <MarketplaceNav />
      <header className="relative overflow-hidden rounded-2xl border border-border bg-bg-elevated px-5 py-7 sm:px-8">
        <div className="pointer-events-none absolute -right-16 -top-24 h-64 w-64 rounded-full bg-[radial-gradient(circle,var(--brand-glow),transparent_68%)]" />
        <p className="text-2xs font-semibold uppercase tracking-[0.2em] text-brand">SessionFS Marketplace</p>
        <h1 className="mt-2 max-w-3xl text-2xl font-semibold tracking-tight text-text-primary sm:text-3xl">Portable agent capabilities, governed before they run.</h1>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-text-secondary">Discover immutable skills and plugins, inspect provenance and degradation, then download locally or request an instruction-only MCP grant.</p>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {canPublish ? (
            <Button size="sm" onClick={() => setPublishOpen(true)}>Publish package</Button>
          ) : me.data ? (
            <p className="text-xs text-text-tertiary">
              Publishing needs the Starter plan or above, or an owner/admin role in an organization with the team skill library.{' '}
              <Link to="/settings/billing" className="font-semibold text-brand hover:underline">View plans</Link>
            </p>
          ) : null}
        </div>
        <div className="mt-6 grid gap-3 sm:grid-cols-[1fr_220px]">
          <Input aria-label="Search marketplace" placeholder="Search by package, namespace, or capability" value={query} onChange={(event) => setQuery(event.target.value)} />
          <Select aria-label="Filter marketplace by tool" value={tool} onValueChange={setTool} options={TOOL_OPTIONS} />
        </div>
        <CategoryBrowse
          categories={categories.data?.items || []}
          active={filters.category}
          onChange={(category) => setFilters((current) => ({ ...current, category }))}
        />
        <FilterRail filters={filters} onChange={setFilters} />
      </header>

      <RevocationIncompleteNotice error={revocations.error} hasPrevious={!!revocations.data} className="mt-4" />
      <UseInYourTool />

      <div className="mt-6 flex items-baseline justify-between">
        <h2 className="text-lg font-semibold text-text-primary">Available to your identity</h2>
        <span className="text-sm text-text-tertiary">
          {loadedCount} {loadedCount === 1 ? 'package' : 'packages'}{packages.hasNextPage ? ' loaded' : ''}
        </span>
      </div>
      {packages.isLoading && (
        <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-label="Loading marketplace">
          {[0, 1, 2].map((item) => <div key={item} className="h-44 animate-pulse rounded-xl border border-border bg-bg-elevated" />)}
        </div>
      )}
      {packages.error && <p role="alert" className="mt-5 rounded-lg border border-danger/30 bg-danger/6 p-4 text-sm text-danger">Marketplace unavailable: {errorMessage(packages.error)}</p>}
      {packages.data && loadedCount === 0 && (
        <EmptyState title="No packages match" description="Try a broader search or another target tool. Private organization packages appear only for authorized members." />
      )}
      {loadedCount > 0 && (
        <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((item) => (
            <PackageCard key={item.listing_id} item={item} state={listingState(item, revocations.data)} onOpen={() => setSelected(item)} />
          ))}
        </div>
      )}
      {packages.hasNextPage && (
        <div className="mt-5 flex justify-center">
          <Button
            variant="secondary"
            onClick={() => void packages.fetchNextPage()}
            loading={packages.isFetchingNextPage}
            disabled={packages.isFetchingNextPage}
          >
            Load more
          </Button>
        </div>
      )}

      <ManagedLibrary />
      {selected && <PackageDialog item={selected} state={listingState(selected, revocations.data)} onClose={() => setSelected(null)} />}
      {publishOpen && <PublishDialog onClose={() => setPublishOpen(false)} />}
    </div>
  );
}
