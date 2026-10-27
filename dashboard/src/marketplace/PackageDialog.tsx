import { useState, type ReactNode } from 'react';
import type { MarketplacePackage } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { Button, Dialog, DialogFooter, IconButton, Input, Select, TableShell, Tabs, TabPanel } from '../components/ui';
import { useCreateSkillInstallRequest, useDeletePersonalSkillVersion, useMarketplacePreview, useMarketplaceReadme } from '../hooks/useMarketplace';
import { useMarketplaceDisclosure } from '../hooks/useMarketplaceDisclosure';
import { useMe } from '../hooks/useMe';
import { useProjects } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { formatBytes } from '../utils/format';
import { TOOL_OPTIONS } from './browseFilterOptions';
import { CAPABILITIES, VISIBILITY_LABELS, elevatedCapabilities } from './capabilities';
import { DeprecationNotice, FreshnessBadges, ToolFreshnessTable } from './discoveryParts';
import { installsLabel } from './discoveryUtils';
import { DisclosureCard } from './DisclosureCard';
import { disclosureAlerts, scanSummary } from './disclosureAlerts';
import { AlertIcon, CloseIcon, ShieldCheckIcon } from './icons';
import { STATE_COPY, type ModerationState } from './moderationState';
import { WorksInMatrix } from './NativeCatalogs';
import { ModerationStateBadge } from './parts';
import { ListingReviewBadge, ProvenancePanel, PublisherBadge } from './publisherParts';
import ReportDialog from './ReportDialog';
import { SafeReadme } from './SafeReadme';

function shortHash(value: string) {
  return value.length > 24 ? `${value.slice(0, 19)}…` : value;
}

function SectionHeading({ id, children }: { id: string; children: ReactNode }) {
  return <h3 id={id} className="text-sm font-semibold text-text-primary">{children}</h3>;
}

/** Capabilities beyond instructions: the present ones up front, the rest in one quiet line. */
function CapabilityList({ item }: { item: MarketplacePackage }) {
  const present = elevatedCapabilities(item.risk);
  const absent = CAPABILITIES.filter((capability) => !capability.present(item.risk));
  return (
    <section aria-labelledby="capabilities-heading">
      <SectionHeading id="capabilities-heading">What it can do</SectionHeading>
      {present.length === 0 ? (
        <p className="mt-2 flex items-start gap-2 text-sm text-text-secondary">
          <ShieldCheckIcon size={16} className="mt-0.5 text-accent" />
          Instructions only. It ships no scripts, MCP servers, hooks, executable extensions or pre-approved tools.
        </p>
      ) : (
        <>
          <ul className="mt-2 divide-y divide-border border-y border-border">
            {present.map((capability) => {
              const detail = capability.detail(item.risk);
              return (
                <li key={capability.key} className="py-2.5">
                  <div className="flex items-center justify-between gap-3 text-sm">
                    <span className="inline-flex items-center gap-2 font-medium text-text-primary">
                      <AlertIcon size={14} className="text-warning" />
                      {capability.label}
                    </span>
                    <span className="text-xs text-warning">Asks for consent</span>
                  </div>
                  {detail && <p className="mt-1 break-all pl-6 font-mono text-xs text-text-secondary">{detail}</p>}
                </li>
              );
            })}
          </ul>
          {absent.length > 0 && (
            <p className="mt-2 text-xs text-text-tertiary">Not included: {absent.map((capability) => capability.label).join(', ')}.</p>
          )}
        </>
      )}
    </section>
  );
}

function DeclaredBehavior({ item }: { item: MarketplacePackage }) {
  const declared = Object.entries(item.risk.disclosed_metadata || {}).filter(([, fields]) => Object.keys(fields || {}).length > 0);
  if (declared.length === 0) return null;
  return (
    <section className="mt-6" aria-labelledby="declared-behavior-heading">
      <SectionHeading id="declared-behavior-heading">Declared behavior</SectionHeading>
      <p className="mt-1 text-xs text-text-tertiary">Settings in the skill files that your tool applies when the skill runs (context, agent, model, effort, shell). Shown for your information; they don&apos;t need consent.</p>
      <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
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

function SourceDetails({ item }: { item: MarketplacePackage }) {
  const rows: [string, ReactNode, string?][] = [
    ['Version', <span className="font-mono">{item.version || 'unversioned'} · {item.version_id}</span>],
    ['Content hash', <span className="font-mono" title={item.content_hash}>{shortHash(item.content_hash)}</span>],
    ['Source', <span className="break-all">{item.source.type}: {item.source.locator}</span>],
    ['Commit', <span className="font-mono">{item.source.commit || 'not pinned by publisher'}</span>],
    ['License', item.license.value || 'Not declared', item.license.declared ? '' : 'text-warning'],
    ['Archive size', formatBytes(item.blob_size_bytes)],
  ];
  return (
    <section className="mt-6" aria-labelledby="source-heading">
      <SectionHeading id="source-heading">Source</SectionHeading>
      <dl className="mt-2 divide-y divide-border border-y border-border text-sm">
        {rows.map(([label, value, tone]) => (
          <div key={label} className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4">
            <dt className="shrink-0 text-xs text-text-tertiary sm:w-32 sm:pt-0.5">{label}</dt>
            <dd className={`min-w-0 ${tone || 'text-text-primary'}`}>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Compatibility({ item, deliverable }: { item: MarketplacePackage; deliverable: boolean }) {
  return (
    <>
      <WorksInMatrix versionId={item.version_id} enabled={deliverable} />
      <section className="mt-6 first:mt-0" aria-labelledby="compatibility-heading">
        <SectionHeading id="compatibility-heading">What each tool gets</SectionHeading>
        <p className="mt-1 text-xs text-text-tertiary">When installed with <span className="font-mono">sfs skill</span>.</p>
        <TableShell minWidth={480} bordered wrapperClassName="mt-2" label="What each tool gets table">
          <thead className="bg-bg-sunken text-xs text-text-tertiary">
            <tr><th className="px-3 py-2 font-medium">Tool</th><th className="px-3 py-2 font-medium">Instruction files</th><th className="px-3 py-2 font-medium">Left out</th></tr>
          </thead>
          <tbody>
            {Object.entries(item.compatibility).map(([tool, state]) => (
              <tr key={tool} className="border-t border-border">
                <td className="px-3 py-2 font-mono text-text-primary">{tool}</td>
                <td className="px-3 py-2 text-text-secondary">{state.instruction_files}</td>
                <td className="px-3 py-2 text-text-secondary">{state.omitted_components.join(', ') || 'Nothing'}</td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      </section>
      <ToolFreshnessTable item={item} />
    </>
  );
}

type DisclosureQuery = ReturnType<typeof useMarketplaceDisclosure>;

/** Scanner result and every disclosure alert, shown on Overview. */
function TrustSummary({ disclosure, deliverable }: { disclosure: DisclosureQuery; deliverable: boolean }) {
  if (!deliverable) return null;
  if (disclosure.isLoading) return <p className="mb-6 text-sm text-text-tertiary">Checking scan results…</p>;
  if (disclosure.error) return <p role="alert" className="mb-6 text-sm text-warning">Scan results unavailable: {errorMessage(disclosure.error)}</p>;
  if (!disclosure.data) return null;
  const alerts = disclosureAlerts(disclosure.data);
  const severity = disclosure.data.scan?.max_severity;
  const scanTone = severity === 'critical' ? 'text-danger' : severity === 'high' || severity === 'medium' ? 'text-warning' : 'text-text-secondary';
  return (
    <section className="mb-6" aria-labelledby="checks-heading" data-testid="trust-summary">
      <SectionHeading id="checks-heading">Checks</SectionHeading>
      <p className={`mt-2 flex items-start gap-2 text-sm ${scanTone}`}>
        <ShieldCheckIcon size={16} className="mt-0.5" />
        {scanSummary(disclosure.data)}
      </p>
      {alerts.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {alerts.map((alert) => (
            <li
              key={alert.key}
              role={alert.tone === 'danger' ? 'alert' : undefined}
              className={`flex items-start gap-2 text-sm ${alert.tone === 'danger' ? 'text-danger' : 'text-warning'}`}
              data-testid={`trust-alert-${alert.key}`}
            >
              <AlertIcon size={16} className="mt-0.5" />
              {alert.text}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** The install disclosure card for the version (marketplace v2 §3.3). */
function PackageDisclosure({ disclosure, enabled }: { disclosure: DisclosureQuery; enabled: boolean }) {
  if (!enabled) return <p className="text-sm text-text-tertiary">Install details are shown only for versions that can be installed.</p>;
  if (disclosure.isLoading) return <p className="text-sm text-text-tertiary">Loading install details…</p>;
  if (disclosure.error) return <p role="alert" className="text-sm text-warning">Install details unavailable: {errorMessage(disclosure.error)}</p>;
  if (!disclosure.data) return <p className="text-sm text-text-tertiary">No install details for this version.</p>;
  return <DisclosureCard disclosure={disclosure.data} />;
}

function ReadmeTab({ item, enabled }: { item: MarketplacePackage; enabled: boolean }) {
  const readme = useMarketplaceReadme(item.package_id, item.version_id, enabled);
  if (readme.isLoading) return <p className="text-sm text-text-tertiary">Loading README…</p>;
  if (readme.error) return <p role="alert" className="text-sm text-warning">README unavailable: {errorMessage(readme.error)}</p>;
  if (!readme.data) return <p className="text-sm text-text-tertiary">This package has no README.</p>;
  return <SafeReadme readme={readme.data} versionId={item.version_id} />;
}

type DetailTab = 'overview' | 'install' | 'compatibility' | 'readme';

export function PackageDialog({ item, state, onClose }: { item: MarketplacePackage; state: ModerationState; onClose: () => void }) {
  const [tab, setTab] = useState<DetailTab>('overview');
  const [showPreview, setShowPreview] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const deliverable = state === 'active';
  const [requestOpen, setRequestOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const preview = useMarketplacePreview(item.version_id, showPreview);
  // Fetched when the dialog opens: Overview shows its scanner result and alerts.
  const disclosure = useMarketplaceDisclosure(item.version_id, [], 'project', deliverable);
  const alertCount = deliverable && disclosure.data ? disclosureAlerts(disclosure.data).length : 0;
  const { auth } = useAuth();
  const { addToast } = useToast();
  const me = useMe();
  const deleteVersion = useDeletePersonalSkillVersion();
  const ownsVersion = item.visibility === 'user' && !!item.owner_user_id && item.owner_user_id === me.data?.user_id;

  async function removeVersion() {
    try {
      await deleteVersion.mutateAsync({ packageId: item.package_id, versionId: item.version_id });
      addToast('success', `Deleted version ${item.version || item.version_id}.`);
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
      addToast('success', `Downloaded version ${item.version || item.version_id}`);
    } catch (error) {
      addToast('error', `Download failed: ${errorMessage(error)}`);
    } finally {
      setDownloading(false);
    }
  }

  const meta = [
    <span key="v" className="font-mono">{item.version || 'unversioned'}</span>,
    <span key="l">{item.license.value || 'No license declared'}</span>,
    <span key="i">{installsLabel(item.installs?.total)} installs</span>,
    ...(item.visibility !== 'public' ? [<span key="vis">{VISIBILITY_LABELS[item.visibility] ?? item.visibility}</span>] : []),
  ];

  return (
    <>
      <Dialog open onClose={onClose} titleId="marketplace-package-title" className="!max-w-3xl max-h-[90vh] overflow-y-auto !p-0">
        <div className="p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate font-mono text-xs text-text-tertiary">{item.collection} / {item.namespace}</p>
              <h2 id="marketplace-package-title" className="mt-1 text-xl font-semibold tracking-tight text-text-primary">{item.display_name}</h2>
            </div>
            <IconButton label="Close" size="sm" onClick={onClose} className="-mr-1 shrink-0">
              <CloseIcon size={16} />
            </IconButton>
          </div>
          {item.description && <p className="mt-2 max-w-2xl text-sm leading-6 text-text-secondary">{item.description}</p>}
          <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs text-text-tertiary">
            <PublisherBadge item={item} />
            <ListingReviewBadge item={item} />
            <ModerationStateBadge state={state} tint={false} />
            <FreshnessBadges item={item} />
            {meta.map((part, index) => (
              <span key={index} className="inline-flex items-center gap-2">
                {index > 0 && <span aria-hidden="true" className="opacity-40">·</span>}
                {part}
              </span>
            ))}
          </div>

          {state !== 'active' && (
            <div role="status" className={`mt-4 rounded-lg border p-3 text-sm ${state === 'blocked' ? 'border-danger/40 bg-danger/6 text-danger' : 'border-warning/40 bg-warning/6 text-warning'}`}>
              <p className="font-semibold">This version is {STATE_COPY[state].label}.</p>
              <p className="mt-1 text-xs text-text-secondary">{STATE_COPY[state].help}</p>
            </div>
          )}
          {item.deprecation && <div className="mt-4"><DeprecationNotice item={item} /></div>}

          <div className="mt-5">
            <Tabs
              bare
              idPrefix="mp-item"
              aria-label="Package details"
              tabs={[
                { key: 'overview', label: 'Overview' },
                {
                  key: 'install',
                  label: alertCount > 0 ? (
                    <span className="inline-flex items-center gap-1.5">
                      Install details
                      <AlertIcon size={13} className="text-warning" />
                      <span className="sr-only">({alertCount} {alertCount === 1 ? 'alert' : 'alerts'})</span>
                    </span>
                  ) : 'Install details',
                },
                { key: 'compatibility', label: 'Compatibility' },
                { key: 'readme', label: 'README' },
              ]}
              activeKey={tab}
              onChange={(key) => setTab(key as DetailTab)}
            />
          </div>
          <TabPanel idPrefix="mp-item" tabKey={tab} className="mt-1 rounded-sm pt-4 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
            {tab === 'overview' && (
              <>
                <TrustSummary disclosure={disclosure} deliverable={deliverable} />
                <CapabilityList item={item} />
                <DeclaredBehavior item={item} />
                <SourceDetails item={item} />
                <ProvenancePanel item={item} />
              </>
            )}
            {tab === 'install' && <PackageDisclosure disclosure={disclosure} enabled={deliverable} />}
            {tab === 'compatibility' && <Compatibility item={item} deliverable={deliverable} />}
            {tab === 'readme' && (deliverable
              ? <ReadmeTab item={item} enabled={deliverable} />
              : <p className="text-sm text-text-tertiary">The README is shown only for versions that can be installed.</p>)}
          </TabPanel>

          {showPreview && (
            <section className="mt-6 border-t border-border pt-5" aria-live="polite" aria-labelledby="preview-heading">
              <h3 id="preview-heading" className="text-sm font-semibold text-warning">Untrusted instruction preview</h3>
              <p className="mt-1 text-xs text-text-secondary">Reading a preview does not approve, install or run the package.</p>
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
            <section className="mt-6 rounded-lg border border-danger/40 bg-danger/6 p-4" role="alertdialog" aria-labelledby="delete-version-title">
              <h3 id="delete-version-title" className="text-sm font-semibold text-danger">Delete this version?</h3>
              <p className="mt-1 text-xs text-text-secondary">Removes the archive and listing for {item.version || item.version_id}. This is refused while an approved install request or organization assignment still uses it.</p>
              <div className="mt-3 flex gap-2">
                <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>Keep it</Button>
                <Button size="sm" variant="danger" onClick={removeVersion} loading={deleteVersion.isPending}>Delete version</Button>
              </div>
            </section>
          )}
        </div>
        <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-2 border-t border-border bg-overlay px-5 py-3 sm:px-6">
          <div className="flex flex-wrap gap-1">
            <Button size="sm" variant="ghost" onClick={() => setShowPreview((value) => !value)} disabled={!deliverable}>
              {showPreview ? 'Hide preview' : 'Preview instructions'}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setReportOpen(true)}>Report…</Button>
            {ownsVersion && !confirmDelete && (
              <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(true)}>Delete version…</Button>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" onClick={download} loading={downloading} disabled={!deliverable}>Download archive</Button>
            <Button size="sm" onClick={() => setRequestOpen(true)} disabled={!deliverable}>Request for project</Button>
          </div>
        </div>
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
      addToast('success', 'Install request sent for approval.');
      onClose();
    } catch (error) {
      addToast('error', `Request failed: ${errorMessage(error)}`);
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="install-request-title">
      <h2 id="install-request-title" className="text-lg font-semibold text-text-primary">Request {item.display_name}</h2>
      <p className="mt-1 mb-5 text-sm text-text-secondary">
        A project admin approves this exact version. Only its instructions load; no files are written and no scripts, hooks or MCP servers run on any device.
      </p>
      <form onSubmit={submit} className="space-y-4">
        {projects.isLoading ? <p className="text-sm text-text-tertiary">Loading projects…</p> : (
          <Select title="Project" value={projectId} onValueChange={setProjectId} options={projectOptions} placeholder="Choose a project" />
        )}
        <Select title="Tool" value={tool} onValueChange={setTool} options={TOOL_OPTIONS.slice(1)} />
        <Select
          title="Who gets it"
          value={scope}
          onValueChange={(value) => setScope(value as typeof scope)}
          options={[
            { value: 'project', label: 'Everyone on the project' },
            { value: 'user', label: 'Just me' },
            { value: 'device', label: 'One of my devices' },
          ]}
        />
        {scope === 'device' && <Input title="Device ID" value={deviceId} onChange={(event) => setDeviceId(event.target.value)} required maxLength={100} />}
        {projectOptions.length === 0 && !projects.isLoading && (
          <p role="alert" className="text-sm text-warning">Create or join a project before requesting a package.</p>
        )}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!projectId || (scope === 'device' && !deviceId.trim())} loading={create.isPending}>Send request</Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

