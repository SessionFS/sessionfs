import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Combobox,
  Dialog,
  DialogFooter,
  DialogHeader,
  Input,
  Select,
  Table,
  Tabs,
} from '../components/ui';
import { useProjects } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { confluenceTemplateLabel, confluenceTemplateOptions } from '../utils/confluence';
import {
  useConfluenceConnection,
  useConfluenceHealth,
  useConfluenceMappings,
  useConfluencePageSearch,
  useConfluenceSpaceSearch,
  useDeleteConfluenceConnection,
  useDeleteConfluenceMapping,
  useSaveConfluenceConnection,
  useSaveConfluenceMapping,
} from './useConfluence';
import type { ConfluenceMapping } from '../api/client';
import {
  ConnectionState,
  IntegrationSection,
  ProblemList,
  SectionStack,
  SiteHost,
  SummaryList,
} from './integrations/parts';
import { confluenceProblems, type ConfluenceSection } from './integrations/health';

export type { ConfluenceSection };

const ENTERPRISE_TIER = 'enterprise';

interface ConfluenceCardProps {
  orgId: string;
  tier: string;
  /** Controlled detail section (the org page keeps it in the URL). */
  section?: ConfluenceSection | null;
  onSectionChange?: (section: ConfluenceSection) => void;
}

/**
 * Org Settings → Integrations → Confluence. Enterprise-only, owner/admin only
 * (the tab itself is admin-gated in OrgPage; this card renders the upgrade
 * note for admins whose org is not on an Enterprise plan).
 */
export function ConfluenceCard({ orgId, tier, section, onSectionChange }: ConfluenceCardProps) {
  if (tier !== ENTERPRISE_TIER) {
    return (
      <Card level="elevated" className="p-5">
        <h2 className="text-lg font-semibold text-text-primary mb-2">Confluence Integration</h2>
        <Alert variant="info" title="Available on Enterprise">
          Publishing SessionFS wiki pages and context documents to Confluence Cloud requires the
          Enterprise plan. Upgrade to connect your Confluence Cloud site, map projects, and publish
          from the project's wiki and context.
        </Alert>
      </Card>
    );
  }

  return <EnterpriseConfluenceCard orgId={orgId} section={section} onSectionChange={onSectionChange} />;
}

const SECTION_LABELS: Record<ConfluenceSection, string> = {
  overview: 'Overview',
  connection: 'Connection',
  mappings: 'Mappings',
};

function EnterpriseConfluenceCard({
  orgId,
  section,
  onSectionChange,
}: {
  orgId: string;
  section?: ConfluenceSection | null;
  onSectionChange?: (section: ConfluenceSection) => void;
}) {
  const connection = useConfluenceConnection(orgId);
  const data = connection.data;
  const connected = data?.connected === true;
  const [localSection, setLocalSection] = useState<ConfluenceSection | null>(null);

  const sections: ConfluenceSection[] = connected ? ['overview', 'connection', 'mappings'] : ['connection'];
  // Controlled (the org page keeps the section in the URL): the prop alone
  // decides, so Back/Forward land on the right section. Uncontrolled, the
  // prop is only the starting section.
  const controlled = onSectionChange !== undefined;
  const requested = controlled ? section ?? null : localSection ?? section ?? null;
  const active: ConfluenceSection = requested && sections.includes(requested) ? requested : sections[0];
  const containerRef = useRef<HTMLDivElement>(null);
  const focusTabRef = useRef(false);
  const go = (next: ConfluenceSection, focusTab = false) => {
    focusTabRef.current = focusTab;
    setLocalSection(next);
    onSectionChange?.(next);
  };
  // A fix action swaps the panel it was in; hand focus to the tab it opened.
  useEffect(() => {
    if (!focusTabRef.current) return;
    focusTabRef.current = false;
    containerRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus();
  }, [active]);

  const content: Record<ConfluenceSection, ReactNode> = {
    overview: <OverviewSection orgId={orgId} go={(next) => go(next, true)} />,
    connection: <ConnectionSection orgId={orgId} />,
    mappings: <MappingsSection orgId={orgId} />,
  };

  return (
    <div ref={containerRef}>
      <div className="mb-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 tabIndex={-1} data-integration-heading className="text-lg font-semibold text-text-primary outline-none">Confluence</h2>
          {connection.isLoading ? null : connected ? (
            <span className="inline-flex flex-wrap items-center gap-x-1.5 text-xs text-text-secondary">
              <ConnectionState connected label="Connected to" />
              <SiteHost url={data!.site_url} />
              {data?.last_verified_at && (
                <span className="text-text-tertiary">
                  · verified {new Date(data.last_verified_at).toLocaleDateString()}
                </span>
              )}
            </span>
          ) : (
            <ConnectionState connected={false} />
          )}
        </div>
        <p className="mt-1 text-sm text-text-tertiary">
          Publish SessionFS wiki pages and the compiled context document to Confluence Cloud. The
          connection is one-way: SessionFS creates and updates the page; edits made in Confluence
          never write back into the project.
        </p>
      </div>

      {connection.isError && (
        <Alert variant="danger" className="mb-4">
          {errorMessage(connection.error, 'Could not load the Confluence connection')}
        </Alert>
      )}

      {sections.length > 1 ? (
        <Tabs
          aria-label="Confluence sections"
          tabs={sections.map((key) => ({ key, label: SECTION_LABELS[key], content: content[key] }))}
          activeKey={active}
          onChange={(key) => go(key as ConfluenceSection)}
        />
      ) : (
        content[active]
      )}
    </div>
  );
}

/* ── Overview: health first ── */

function OverviewSection({ orgId, go }: { orgId: string; go: (section: ConfluenceSection) => void }) {
  const { data: connection } = useConfluenceConnection(orgId);
  const { data: mappings } = useConfluenceMappings(orgId);
  const health = useConfluenceHealth(orgId);

  return (
    <SectionStack>
      <IntegrationSection title="Health" label="Confluence health">
        {health.isLoading ? (
          <div className="text-sm text-text-tertiary py-2">Loading…</div>
        ) : health.isError ? (
          <Alert variant="danger">{errorMessage(health.error, 'Could not load Confluence health')}</Alert>
        ) : (
          <ProblemList
            label="Confluence problems"
            problems={confluenceProblems(health.data!, go)}
            allGood="All good: every project is mapped and nothing failed to publish."
          />
        )}
      </IntegrationSection>
      <IntegrationSection title="At a glance">
        <SummaryList
          items={[
            { label: 'Mapped projects', value: mappings ? `${mappings.length}` : '—' },
            {
              label: 'Default template',
              value: connection?.default_template ? confluenceTemplateLabel(connection.default_template) : '—',
            },
            { label: 'Batch window', value: `${connection?.batch_window_minutes ?? 15} minutes` },
          ]}
        />
      </IntegrationSection>
    </SectionStack>
  );
}

/* ── Connection ── */

function ConnectionSection({ orgId }: { orgId: string }) {
  const { data } = useConfluenceConnection(orgId);
  const save = useSaveConfluenceConnection(orgId);
  const disconnect = useDeleteConfluenceConnection(orgId);
  const { addToast } = useToast();

  const connected = data?.connected === true;
  const [editing, setEditing] = useState(false);
  // The form and the summary swap places, so move focus with them: into the
  // first field on edit, back to "Edit connection" on save or cancel.
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);
  const closeEditor = () => {
    returnFocusRef.current = true;
    setEditing(false);
  };
  useEffect(() => {
    if (editing || !returnFocusRef.current) return;
    returnFocusRef.current = false;
    editButtonRef.current?.focus();
  }, [editing, connected]);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [disconnectError, setDisconnectError] = useState<string | null>(null);

  async function handleDisconnect() {
    try {
      await disconnect.mutateAsync();
      setConfirmOpen(false);
      setDisconnectError(null);
      setEditing(false);
      addToast('success', 'Confluence disconnected');
    } catch (err: unknown) {
      setConfirmOpen(false);
      setDisconnectError(errorMessage(err, 'Could not disconnect Confluence'));
    }
  }

  const showForm = !connected || editing;

  return (
    <section aria-label="Confluence connection">
      <h3 className="sr-only">Connection</h3>
      {disconnectError && (
        <Alert variant="danger" className="mb-4">
          {disconnectError}
        </Alert>
      )}

      {showForm ? (
        <ConnectionForm
          // Remount with fresh values when the connection loads or changes, so
          // the stored site/email/template/batch window prefill without an effect
          // (the token is write-only and always starts blank).
          key={`${connected}:${data?.site_url ?? ''}:${data?.account_email ?? ''}:${data?.default_template ?? ''}:${data?.batch_window_minutes ?? 15}`}
          connected={connected}
          initialSiteUrl={data?.site_url ?? ''}
          initialEmail={data?.account_email ?? ''}
          initialTemplate={data?.default_template ?? ''}
          initialBatchWindow={data?.batch_window_minutes ?? 15}
          savePending={save.isPending}
          onSave={save.mutateAsync}
          onSaved={closeEditor}
          onCancel={connected ? closeEditor : undefined}
        />
      ) : (
        <>
          <SummaryList
            items={[
              { label: 'Site', value: <span className="font-mono text-xs" title={data!.site_url}>{data!.site_url}</span> },
              { label: 'Account', value: data!.account_email ?? '—' },
              { label: 'API token', value: <span className="font-mono text-xs">token {data!.token_hint ?? '••••'}</span> },
              {
                label: 'Verified',
                value: data?.last_verified_at ? new Date(data.last_verified_at).toLocaleDateString() : '—',
              },
              {
                label: 'Default template',
                value: data?.default_template ? confluenceTemplateLabel(data.default_template) : '—',
              },
              { label: 'Batch window', value: `${data?.batch_window_minutes ?? 15} minutes` },
            ]}
          />
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button ref={editButtonRef} variant="secondary" onClick={() => setEditing(true)}>
              Edit connection
            </Button>
            <Button variant="ghost" onClick={() => setConfirmOpen(true)}>
              Disconnect
            </Button>
          </div>
          <p className="mt-3 text-xs text-text-tertiary">
            The API token is stored encrypted and never shown again. Editing the connection asks for
            it so it can be re-encrypted.
          </p>
        </>
      )}

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} titleId="confluence-disconnect-heading">
        <DialogHeader titleId="confluence-disconnect-heading">Disconnect Confluence?</DialogHeader>
        <p className="text-sm text-text-secondary">
          SessionFS will delete the stored Atlassian credential. Existing Confluence pages and
          mappings are left untouched, and publishes will fail until the org reconnects.
        </p>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
            Cancel
          </Button>
          <Button variant="danger" disabled={disconnect.isPending} onClick={handleDisconnect}>
            {disconnect.isPending ? 'Disconnecting…' : 'Disconnect'}
          </Button>
        </DialogFooter>
      </Dialog>
    </section>
  );
}

interface ConnectionFormProps {
  connected: boolean;
  initialSiteUrl: string;
  initialEmail: string;
  initialTemplate: string;
  initialBatchWindow: number;
  savePending: boolean;
  onSave: ReturnType<typeof useSaveConfluenceConnection>['mutateAsync'];
  onSaved: () => void;
  /** Present while editing an existing connection. */
  onCancel?: () => void;
}

function ConnectionForm({
  connected,
  initialSiteUrl,
  initialEmail,
  initialTemplate,
  initialBatchWindow,
  savePending,
  onSave,
  onSaved,
  onCancel,
}: ConnectionFormProps) {
  const { addToast } = useToast();
  const [siteUrl, setSiteUrl] = useState(initialSiteUrl);
  const [email, setEmail] = useState(initialEmail);
  const [token, setToken] = useState('');
  const [template, setTemplate] = useState(initialTemplate);
  const [batchWindow, setBatchWindow] = useState(String(initialBatchWindow));
  const [error, setError] = useState<string | null>(null);

  const batchWindowNumber = Number(batchWindow);
  const formReady =
    siteUrl.trim() !== '' &&
    email.trim() !== '' &&
    token.trim() !== '' &&
    template !== '' &&
    Number.isInteger(batchWindowNumber) &&
    batchWindowNumber >= 1 &&
    batchWindowNumber <= 1440;

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await onSave({
        site_url: siteUrl.trim(),
        account_email: email.trim(),
        api_token: token,
        default_template: template,
        batch_window_minutes: batchWindowNumber,
      });
      setToken('');
      addToast('success', connected ? 'Confluence connection updated' : 'Confluence connected');
      onSaved();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not save the Confluence connection'));
    }
  }

  return (
    <form onSubmit={handleSave} className="space-y-3 max-w-md" autoComplete="off">
      <Input
        title="Site URL"
        type="url"
        // Opened from "Edit connection": start in the first field.
        autoFocus={!!onCancel}
        value={siteUrl}
        onChange={(e) => setSiteUrl(e.target.value)}
        placeholder="https://acme.atlassian.net"
        hint="A Confluence Cloud site URL, e.g. https://acme.atlassian.net"
        required
      />
      <Input
        title="Account email"
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="confluence@acme.com"
        autoComplete="off"
        required
      />
      <Input
        title="API token"
        type="password"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        placeholder={connected ? 'Enter a token to replace the stored one' : 'Atlassian API token'}
        autoComplete="new-password"
        required
        hint="Stored encrypted and never shown again; saving the connection always asks for it so it can be re-encrypted."
      />
      <Select
        title="Default template"
        value={template}
        onValueChange={setTemplate}
        options={confluenceTemplateOptions()}
        placeholder="Select a template"
      />
      <Input
        title="Batch window (minutes)"
        type="number"
        min={1}
        max={1440}
        value={batchWindow}
        onChange={(e) => setBatchWindow(e.target.value)}
        hint="How long the publish sweep may take to finish a publish before it is marked unknown."
        required
      />
      {error && (
        <Alert variant="danger">
          {error}
        </Alert>
      )}
      <div className="flex flex-wrap items-center gap-3 pt-1">
        <Button type="submit" disabled={savePending || !formReady}>
          {savePending ? 'Saving…' : connected ? 'Save connection' : 'Connect Confluence'}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}

/* ── Mappings ── */

type MappingDialogState =
  | { mode: 'new' }
  | { mode: 'edit'; mapping: ConfluenceMapping }
  | { mode: 'remove'; mapping: ConfluenceMapping };

function publishLabel(m: ConfluenceMapping): string {
  if (m.publish_context && m.auto_publish_pages) return 'Context + pages';
  if (m.publish_context) return 'Context';
  if (m.auto_publish_pages) return 'Pages (auto)';
  return 'Manual only';
}

function MappingsSection({ orgId }: { orgId: string }) {
  const { data: mappings, isLoading } = useConfluenceMappings(orgId);
  const { data: projects, isLoading: orgProjectsLoading } = useProjects();
  const [dialog, setDialog] = useState<MappingDialogState | null>(null);
  const { addToast } = useToast();

  // `GET /api/v1/projects/` lists every project the caller can read; only the
  // ones owned by this org can be mapped.
  const orgProjects = useMemo(
    () =>
      (projects ?? [])
        .filter((p) => p.org_id === orgId)
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })),
    [projects, orgId],
  );
  const mappedProjectIds = useMemo(
    () => new Set((mappings ?? []).map((m) => m.project_id)),
    [mappings],
  );
  const projectName = (id: string) =>
    orgProjects.find((p) => p.id === id)?.name ?? id;

  const close = () => setDialog(null);

  return (
    <IntegrationSection
      title="Project mappings"
      label="Confluence project mappings"
      description="Which Confluence space and parent page each SessionFS project publishes under."
      actions={
        <Button size="sm" variant="secondary" onClick={() => setDialog({ mode: 'new' })}>
          Add mapping
        </Button>
      }
    >

      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : (
        <Table<ConfluenceMapping>
          label="Confluence project mappings table"
          columns={[
            {
              key: 'project',
              header: 'SessionFS project',
              render: (m) => <span className="text-text-primary">{projectName(m.project_id)}</span>,
            },
            { key: 'space', header: 'Confluence space', render: (m) => <span className="font-mono">{m.space_key}</span> },
            {
              key: 'template',
              header: 'Template',
              render: (m) => (m.template ? confluenceTemplateLabel(m.template) : <span className="text-text-tertiary">Org default</span>),
            },
            { key: 'publish', header: 'Publish', render: (m) => publishLabel(m) },
            {
              key: 'actions',
              header: <span className="sr-only">Actions</span>,
              align: 'right',
              render: (m) => (
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    className="text-xs text-brand hover:underline"
                    onClick={() => setDialog({ mode: 'edit', mapping: m })}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="text-xs text-danger hover:underline"
                    onClick={() => setDialog({ mode: 'remove', mapping: m })}
                  >
                    Remove
                  </button>
                </div>
              ),
            },
          ]}
          data={mappings ?? []}
          rowKey={(m) => m.project_id}
          emptyState="No mappings yet. Map a SessionFS project to a Confluence space to enable publishing."
        />
      )}

      {dialog?.mode === 'new' && (
        <MappingEditorDialog
          orgId={orgId}
          orgProjects={orgProjects}
          orgProjectsLoading={orgProjectsLoading}
          mappedProjectIds={mappedProjectIds}
          onClose={close}
          onSaved={(projectId) => {
            close();
            addToast('success', `Mapping saved for ${projectName(projectId)}`);
          }}
        />
      )}
      {dialog?.mode === 'edit' && (
        <MappingEditorDialog
          orgId={orgId}
          orgProjects={orgProjects}
          orgProjectsLoading={orgProjectsLoading}
          mappedProjectIds={mappedProjectIds}
          initial={dialog.mapping}
          onClose={close}
          onSaved={(projectId) => {
            close();
            addToast('success', `Mapping saved for ${projectName(projectId)}`);
          }}
        />
      )}
      {dialog?.mode === 'remove' && (
        <ConfirmRemoveMappingDialog
          orgId={orgId}
          projectName={projectName(dialog.mapping.project_id)}
          mapping={dialog.mapping}
          onClose={close}
          onRemoved={() => {
            close();
            addToast('success', `Mapping removed for ${projectName(dialog.mapping.project_id)}`);
          }}
        />
      )}
    </IntegrationSection>
  );
}

interface MappingEditorDialogProps {
  orgId: string;
  orgProjects: { id: string; name: string }[];
  orgProjectsLoading: boolean;
  /** Projects that already have a mapping; a new mapping can't pick them. */
  mappedProjectIds: Set<string>;
  initial?: ConfluenceMapping;
  onClose: () => void;
  onSaved: (projectId: string) => void;
}

function MappingEditorDialog({
  orgId,
  orgProjects,
  orgProjectsLoading,
  mappedProjectIds,
  initial,
  onClose,
  onSaved,
}: MappingEditorDialogProps) {
  const save = useSaveConfluenceMapping(orgId);
  const [spaceQuery, setSpaceQuery] = useState('');
  const spaceSearch = useConfluenceSpaceSearch(orgId, spaceQuery);
  const [spaceKey, setSpaceKey] = useState(initial?.space_key ?? '');
  // The picked space's "KEY · Name" label, kept so the trigger still reads
  // well after a later search returns a page without it.
  const [spaceLabel, setSpaceLabel] = useState(initial?.space_key ?? '');
  const [pageQuery, setPageQuery] = useState('');
  // Page search follows the currently picked space, so swapping spaces clears
  // and reloads the parent-page list (an edit starts on its stored space).
  const pageSearch = useConfluencePageSearch(orgId, spaceKey || null, pageQuery);

  const isEdit = initial != null;
  const [projectId, setProjectId] = useState(initial?.project_id ?? '');
  const [parentPageId, setParentPageId] = useState(initial?.parent_page_id ?? '');
  // Same as above: the stored parent page title may not be in a later search.
  const [parentPageLabel, setParentPageLabel] = useState(initial?.parent_page_id ?? '');
  const [template, setTemplate] = useState<string>(initial?.template ?? '');
  const [publishContext, setPublishContext] = useState(initial?.publish_context ?? false);
  const [autoPublishPages, setAutoPublishPages] = useState(initial?.auto_publish_pages ?? false);
  const [error, setError] = useState<string | null>(null);

  const templateOptions = useMemo(
    () => [{ value: '', label: 'Org default' }, ...confluenceTemplateOptions()],
    [],
  );
  // A new mapping offers only unmapped projects; an edit shows its own project.
  const projectOptions = orgProjects
    .filter((p) => (initial ? p.id === initial.project_id : !mappedProjectIds.has(p.id)))
    .map((p) => ({ value: p.id, label: p.name }));
  const projectEmptyMessage = orgProjectsLoading
    ? 'Loading projects…'
    : orgProjects.length === 0
      ? 'No projects in this organization yet — create one first'
      : 'All projects in this organization are already mapped';
  const spaceOptions = (spaceSearch.data?.spaces ?? []).map((s) => ({
    value: s.key,
    label: `${s.key} · ${s.name}`,
  }));
  const spaceFooter = spaceSearch.data?.has_more
    ? spaceSearch.debouncedQuery
      ? `Showing the first ${spaceSearch.data.spaces.length} matches — keep typing to narrow them`
      : `Showing the first ${spaceSearch.data.spaces.length} spaces — type to search`
    : null;
  const pageOptions = (pageSearch.data ?? []).map((p) => ({ value: p.id, label: p.title }));

  // Picking a new space clears the parent-page selection, whose old page no
  // longer belongs to the selected space.
  function handleSpaceChange(key: string, option: { label: string }) {
    setSpaceKey(key);
    setSpaceLabel(option.label);
    if (key !== initial?.space_key) {
      setParentPageId('');
      setParentPageLabel('');
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await save.mutateAsync({
        projectId,
        body: {
          space_key: spaceKey,
          parent_page_id: parentPageId,
          template: template || null,
          publish_context: publishContext,
          auto_publish_pages: autoPublishPages,
        },
      });
      onSaved(projectId);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not save the mapping'));
    }
  }

  const ready = projectId !== '' && spaceKey !== '' && parentPageId !== '';

  return (
    <Dialog open onClose={onClose} titleId="confluence-mapping-editor-title" className="max-w-xl">
      <DialogHeader titleId="confluence-mapping-editor-title">
        {isEdit ? 'Edit mapping' : 'Add mapping'}
      </DialogHeader>
      <form onSubmit={handleSubmit} className="space-y-3" autoComplete="off">
        <Combobox
          title="SessionFS project"
          value={projectId}
          onValueChange={setProjectId}
          options={projectOptions}
          selectedLabel={initial ? orgProjects.find((p) => p.id === initial.project_id)?.name : undefined}
          placeholder="Select a project"
          searchPlaceholder="Search by name or ID"
          emptyMessage={projectEmptyMessage}
          noMatchesMessage={(q) => `No unmapped projects match “${q}”`}
          disabled={isEdit}
        />
        <Combobox
          title="Confluence space"
          value={spaceKey}
          onValueChange={handleSpaceChange}
          options={spaceOptions}
          selectedLabel={spaceLabel}
          placeholder="Select a space"
          searchPlaceholder="Search by key or name"
          onSearchChange={setSpaceQuery}
          loading={spaceSearch.searching && !spaceSearch.isError}
          pending={spaceSearch.searching && !spaceSearch.isError}
          emptyMessage={
            spaceSearch.isError
              ? errorMessage(spaceSearch.error, 'Could not load Confluence spaces')
              : 'No Confluence spaces are visible to the connected account'
          }
          noMatchesMessage={(q) =>
            spaceSearch.isError
              ? errorMessage(spaceSearch.error, 'Could not search Confluence spaces')
              : `No Confluence spaces match “${q}”`
          }
          footer={spaceFooter}
        />
        <Combobox
          title="Parent page"
          value={parentPageId}
          onValueChange={(id, option) => {
            setParentPageId(id);
            setParentPageLabel(option.label);
          }}
          options={pageOptions}
          selectedLabel={parentPageLabel}
          placeholder="Select a parent page"
          searchPlaceholder="Search by title"
          onSearchChange={setPageQuery}
          loading={pageSearch.searching && !pageSearch.isError}
          pending={pageSearch.searching && !pageSearch.isError}
          disabled={!spaceKey}
          emptyMessage={
            !spaceKey
              ? 'Pick a Confluence space first'
              : pageSearch.isError
                ? errorMessage(pageSearch.error, 'Could not load Confluence pages')
                : 'No pages found in this space'
          }
          noMatchesMessage={(q) =>
            pageSearch.isError
              ? errorMessage(pageSearch.error, 'Could not search Confluence pages')
              : `No pages match “${q}”`
          }
          hint="SessionFS pages are created as children of this page (the space's home page is not allowed)."
        />
        <Select
          title="Template override"
          value={template}
          onValueChange={setTemplate}
          options={templateOptions}
          placeholder="Org default"
        />
        <Checkbox
          label="Publish the compiled context document"
          description="Also publish the project's context document alongside its wiki pages."
          checked={publishContext}
          onChange={(e) => setPublishContext(e.target.checked)}
        />
        <Checkbox
          label="Auto-publish pages"
          description="Publish wiki pages automatically once they opt in, without a manual publish."
          checked={autoPublishPages}
          onChange={(e) => setAutoPublishPages(e.target.checked)}
        />

        {error && (
          <Alert variant="danger">
            {error}
          </Alert>
        )}

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={save.isPending || !ready}>
            {save.isPending ? 'Saving…' : 'Save mapping'}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

function ConfirmRemoveMappingDialog({
  orgId,
  projectName,
  mapping,
  onClose,
  onRemoved,
}: {
  orgId: string;
  projectName: string;
  mapping: ConfluenceMapping;
  onClose: () => void;
  onRemoved: () => void;
}) {
  const remove = useDeleteConfluenceMapping(orgId);
  const [error, setError] = useState<string | null>(null);

  async function handleRemove() {
    setError(null);
    try {
      await remove.mutateAsync(mapping.project_id);
      onRemoved();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not remove the mapping'));
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="confluence-mapping-remove-title">
      <DialogHeader titleId="confluence-mapping-remove-title">Remove mapping?</DialogHeader>
      <p className="text-sm text-text-secondary">
        Stop publishing <span className="font-mono">{projectName}</span> to Confluence space{' '}
        <span className="font-mono">{mapping.space_key}</span>. Existing Confluence pages are left
        untouched.
      </p>
      {error && (
        <Alert variant="danger" className="mt-3">
          {error}
        </Alert>
      )}
      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button variant="danger" disabled={remove.isPending} onClick={handleRemove}>
          {remove.isPending ? 'Removing…' : 'Remove'}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
