import { useMemo, useState, type FormEvent } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Dialog,
  DialogFooter,
  DialogHeader,
  Input,
  MonoText,
  Select,
  Table,
} from '../components/ui';
import { useProjects } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { JIRA_TEMPLATES, jiraTemplateLabel } from '../utils/jira';
import {
  useDeleteJiraConnection,
  useDeleteJiraMapping,
  useJiraConnection,
  useJiraHealth,
  useJiraIssueTypes,
  useJiraMappings,
  useJiraProjects,
  useSaveJiraConnection,
  useSaveJiraMapping,
} from './useJira';
import type { JiraMapping } from '../api/client';

const ENTERPRISE_TIER = 'enterprise';

interface JiraCardProps {
  orgId: string;
  tier: string;
}

/**
 * Org Settings → Integrations → Jira. Enterprise-only, owner/admin only
 * (the tab itself is admin-gated in OrgPage; this card renders the upgrade
 * note for admins whose org is not on an Enterprise plan).
 */
export function JiraCard({ orgId, tier }: JiraCardProps) {
  if (tier !== ENTERPRISE_TIER) {
    return (
      <Card level="elevated" className="p-5">
        <h2 className="text-lg font-semibold text-text-primary mb-2">Jira Integration</h2>
        <Alert variant="info" title="Available on Enterprise">
          Promoting SessionFS tickets to Jira Cloud requires the Enterprise plan. Upgrade to connect
          your Jira Cloud site, map projects, and promote tickets from the ticket detail.
        </Alert>
      </Card>
    );
  }

  return <EnterpriseJiraCard orgId={orgId} />;
}

function EnterpriseJiraCard({ orgId }: { orgId: string }) {
  const connection = useJiraConnection(orgId);
  const connected = connection.data?.connected === true;

  return (
    <Card level="elevated" className="p-5">
      <h2 className="text-lg font-semibold text-text-primary mb-2">Jira Integration</h2>
      <p className="text-sm text-text-tertiary mb-4">
        Promote SessionFS tickets to Jira Cloud stories. The connection is one-way: SessionFS creates
        and updates the story; edits made in Jira never write back into the ticket.
      </p>

      {connection.isError && (
        <Alert variant="danger" className="mb-4">
          {errorMessage(connection.error, 'Could not load the Jira connection')}
        </Alert>
      )}

      <ConnectionSection orgId={orgId} />
      {connected && <MappingsSection orgId={orgId} />}
      {connected && <HealthSection orgId={orgId} />}
    </Card>
  );
}

/* ── Connection ── */

function ConnectionSection({ orgId }: { orgId: string }) {
  const { data } = useJiraConnection(orgId);
  const save = useSaveJiraConnection(orgId);
  const disconnect = useDeleteJiraConnection(orgId);
  const { addToast } = useToast();

  const connected = data?.connected === true;
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [disconnectError, setDisconnectError] = useState<string | null>(null);

  async function handleDisconnect() {
    try {
      await disconnect.mutateAsync();
      setConfirmOpen(false);
      setDisconnectError(null);
      addToast('success', 'Jira disconnected');
    } catch (err: unknown) {
      setConfirmOpen(false);
      setDisconnectError(errorMessage(err, 'Could not disconnect Jira'));
    }
  }

  return (
    <section className="mb-6" aria-label="Jira connection">
      <h3 className="text-sm font-semibold text-text-primary mb-2">Connection</h3>

      <div className="flex items-center gap-2 mb-3 text-sm">
        <span
          className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-success' : 'bg-border'}`}
          aria-hidden="true"
        />
        {connected ? (
          <span className="text-text-primary flex flex-wrap items-center gap-x-1.5 min-w-0">
            Connected to <MonoText value={data!.site_url ?? ''} tailChars={10} />
            <span className="text-text-tertiary">
              · token {data!.token_hint ?? '••••'} · verified{' '}
              {data?.last_verified_at ? new Date(data.last_verified_at).toLocaleDateString() : '—'}
            </span>
          </span>
        ) : (
          <span className="text-text-tertiary">Not connected</span>
        )}
      </div>

      {disconnectError && (
        <Alert variant="danger" className="mb-4">
          {disconnectError}
        </Alert>
      )}

      <ConnectionForm
        // Remount with fresh values when the connection loads or changes, so
        // the stored site/email/template prefill without an effect (the token
        // is write-only and always starts blank).
        key={`${connected}:${data?.site_url ?? ''}:${data?.account_email ?? ''}:${data?.default_template ?? ''}`}
        connected={connected}
        initialSiteUrl={data?.site_url ?? ''}
        initialEmail={data?.account_email ?? ''}
        initialTemplate={data?.default_template ?? ''}
        savePending={save.isPending}
        onSave={save.mutateAsync}
        onDisconnect={() => setConfirmOpen(true)}
      />

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} titleId="jira-disconnect-heading">
        <DialogHeader titleId="jira-disconnect-heading">Disconnect Jira?</DialogHeader>
        <p className="text-sm text-text-secondary">
          SessionFS will delete the stored Atlassian credential. Existing Jira issues and mappings are
          left untouched, and promotions will fail until the org reconnects.
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
  savePending: boolean;
  onSave: ReturnType<typeof useSaveJiraConnection>['mutateAsync'];
  onDisconnect: () => void;
}

function ConnectionForm({
  connected,
  initialSiteUrl,
  initialEmail,
  initialTemplate,
  savePending,
  onSave,
  onDisconnect,
}: ConnectionFormProps) {
  const { addToast } = useToast();
  const [siteUrl, setSiteUrl] = useState(initialSiteUrl);
  const [email, setEmail] = useState(initialEmail);
  const [token, setToken] = useState('');
  const [template, setTemplate] = useState(initialTemplate);
  const [error, setError] = useState<string | null>(null);

  const formReady = siteUrl.trim() !== '' && email.trim() !== '' && token.trim() !== '' && template !== '';

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await onSave({
        site_url: siteUrl.trim(),
        account_email: email.trim(),
        api_token: token,
        default_template: template,
      });
      setToken('');
      addToast('success', connected ? 'Jira connection updated' : 'Jira connected');
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not save the Jira connection'));
    }
  }

  return (
    <form onSubmit={handleSave} className="space-y-3 max-w-md" autoComplete="off">
      <Input
        title="Site URL"
        type="url"
        value={siteUrl}
        onChange={(e) => setSiteUrl(e.target.value)}
        placeholder="https://acme.atlassian.net"
        required
      />
      <Input
        title="Account email"
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="jira@acme.com"
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
      />
      <Select
        title="Template"
        value={template}
        onValueChange={setTemplate}
        options={JIRA_TEMPLATES}
        placeholder="Select a template"
      />
      <p className="text-xs text-text-tertiary">
        The API token is stored encrypted and never shown again; updating the connection always asks
        for it so it can be re-encrypted.
      </p>
      {error && (
        <Alert variant="danger">
          {error}
        </Alert>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={savePending || !formReady}>
          {savePending ? 'Saving…' : connected ? 'Update connection' : 'Connect Jira'}
        </Button>
        {connected && (
          <Button type="button" variant="ghost" onClick={onDisconnect}>
            Disconnect
          </Button>
        )}
      </div>
    </form>
  );
}

/* ── Mappings ── */

type MappingDialogState =
  | { mode: 'new' }
  | { mode: 'edit'; mapping: JiraMapping }
  | { mode: 'remove'; mapping: JiraMapping };

function MappingsSection({ orgId }: { orgId: string }) {
  const { data: mappings, isLoading } = useJiraMappings(orgId);
  const { data: projects } = useProjects();
  const [dialog, setDialog] = useState<MappingDialogState | null>(null);
  const { addToast } = useToast();

  const orgProjects = useMemo(
    () => (projects ?? []).filter((p) => p.org_id === orgId),
    [projects, orgId],
  );
  const projectName = (id: string) =>
    orgProjects.find((p) => p.id === id)?.name ?? id;

  const close = () => setDialog(null);

  return (
    <section className="mb-6" aria-label="Jira project mappings">
      <div className="flex items-center justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold text-text-primary">Project mappings</h3>
        <Button size="sm" variant="secondary" onClick={() => setDialog({ mode: 'new' })}>
          Add mapping
        </Button>
      </div>

      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : (
        <Table<JiraMapping>
          columns={[
            {
              key: 'project',
              header: 'SessionFS project',
              render: (m) => <span className="font-mono text-xs">{projectName(m.project_id)}</span>,
            },
            { key: 'key', header: 'Jira project', render: (m) => <span className="font-mono">{m.jira_project_key}</span> },
            { key: 'issue_type', header: 'Issue type', render: (m) => m.issue_type },
            {
              key: 'template',
              header: 'Template',
              render: (m) => (m.template ? jiraTemplateLabel(m.template) : <span className="text-text-tertiary">Org default</span>),
            },
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
          emptyState="No mappings yet. Map a SessionFS project to a Jira project to enable promotion."
        />
      )}

      {dialog?.mode === 'new' && (
        <MappingEditorDialog
          orgId={orgId}
          orgProjects={orgProjects}
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
    </section>
  );
}

interface MappingEditorDialogProps {
  orgId: string;
  orgProjects: { id: string; name: string }[];
  initial?: JiraMapping;
  onClose: () => void;
  onSaved: (projectId: string) => void;
}

function MappingEditorDialog({ orgId, orgProjects, initial, onClose, onSaved }: MappingEditorDialogProps) {
  const save = useSaveJiraMapping(orgId);
  const { data: jiraProjects, isLoading: projectsLoading } = useJiraProjects(orgId);

  const isEdit = initial != null;
  const [projectId, setProjectId] = useState(initial?.project_id ?? '');
  const [jiraKey, setJiraKey] = useState(initial?.jira_project_key ?? '');
  const [issueType, setIssueType] = useState(initial?.issue_type ?? '');
  const [template, setTemplate] = useState<string>(initial?.template ?? '');
  const [doneStatus, setDoneStatus] = useState(initial?.done_status_name ?? '');
  const [error, setError] = useState<string | null>(null);

  const issueTypes = useJiraIssueTypes(orgId, jiraKey || null);
  const projectOptions = orgProjects.map((p) => ({ value: p.id, label: p.name }));
  const jiraOptions = (jiraProjects ?? []).map((p) => ({ value: p.key, label: `${p.key} · ${p.name}` }));
  const issueTypeOptions = (issueTypes.data ?? []).map((t) => ({ value: t.name, label: t.name }));
  // Default to the first remote issue type when the picked one no longer
  // matches (e.g. the admin switched Jira projects and the types reloaded).
  // For an edit, keep the stored type while the remote list is unavailable
  // (loading or failed) so saving never silently rewrites it to 'Story'.
  const effectiveIssueType = issueTypeOptions.some((o) => o.value === issueType)
    ? issueType
    : isEdit && issueTypeOptions.length === 0
      ? issueType
      : issueTypeOptions[0]?.value ?? '';

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await save.mutateAsync({
        projectId,
        body: {
          jira_project_key: jiraKey,
          issue_type: effectiveIssueType || 'Story',
          template: template || null,
          done_status_name: doneStatus.trim() || null,
        },
      });
      onSaved(projectId);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not save the mapping'));
    }
  }

  // Save stays disabled while issue types are still loading so a mapping can
  // never be saved against an incomplete type list (which would silently
  // fall back to 'Story').
  const ready = projectId !== '' && jiraKey !== '' && !issueTypes.isLoading;

  return (
    <Dialog open onClose={onClose} titleId="jira-mapping-editor-title" className="max-w-xl">
      <DialogHeader titleId="jira-mapping-editor-title">
        {isEdit ? 'Edit mapping' : 'Add mapping'}
      </DialogHeader>
      <form onSubmit={handleSubmit} className="space-y-3" autoComplete="off">
        <Select
          title="SessionFS project"
          value={projectId}
          onValueChange={setProjectId}
          options={projectOptions}
          placeholder="Select a project"
          disabled={isEdit}
        />
        <Select
          title="Jira project"
          value={jiraKey}
          onValueChange={setJiraKey}
          options={jiraOptions}
          placeholder={projectsLoading ? 'Loading Jira projects…' : 'Select a Jira project'}
          disabled={projectsLoading}
        />
        <Select
          title="Issue type"
          value={effectiveIssueType}
          onValueChange={setIssueType}
          options={issueTypeOptions}
          placeholder={jiraKey ? 'Loading issue types…' : 'Pick a Jira project first'}
          disabled={!jiraKey || issueTypes.isLoading}
        />
        <Select
          title="Template override"
          value={template}
          onValueChange={setTemplate}
          options={[{ value: '', label: 'Org default' }, ...JIRA_TEMPLATES]}
          placeholder="Org default"
        />
        <Input
          title="Done status (optional)"
          value={doneStatus}
          onChange={(e) => setDoneStatus(e.target.value)}
          placeholder="e.g. Done"
        />

        {error && (
          <Alert variant="danger">
            {error}
          </Alert>
        )}
        {issueTypes.isError && (
          <Alert variant="danger">{errorMessage(issueTypes.error, 'Could not load issue types')}</Alert>
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
  mapping: JiraMapping;
  onClose: () => void;
  onRemoved: () => void;
}) {
  const remove = useDeleteJiraMapping(orgId);
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
    <Dialog open onClose={onClose} titleId="jira-mapping-remove-title">
      <DialogHeader titleId="jira-mapping-remove-title">Remove mapping?</DialogHeader>
      <p className="text-sm text-text-secondary">
        Stop promoting <span className="font-mono">{projectName}</span> to Jira project{' '}
        <span className="font-mono">{mapping.jira_project_key}</span>. Existing Jira issues are left
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

/* ── Health ── */

function HealthSection({ orgId }: { orgId: string }) {
  const { data, isLoading, isError, error } = useJiraHealth(orgId);

  if (isLoading) {
    return (
      <section aria-label="Jira health">
        <h3 className="text-sm font-semibold text-text-primary mb-2">Health</h3>
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      </section>
    );
  }

  if (isError) {
    return (
      <section aria-label="Jira health">
        <h3 className="text-sm font-semibold text-text-primary mb-2">Health</h3>
        <Alert variant="danger">{errorMessage(error, 'Could not load Jira health')}</Alert>
      </section>
    );
  }

  const health = data!;
  const totalIssues =
    health.last_push_failures.length +
    health.stale_links.length +
    health.publishes_in_progress.length +
    health.publishes_unknown.length;

  return (
    <section aria-label="Jira health">
      <h3 className="text-sm font-semibold text-text-primary mb-2">Health</h3>

      {health.unmapped_project_count === 0 && totalIssues === 0 ? (
        <Alert variant="success">All mapped, no push failures, stale links, or pending publishes.</Alert>
      ) : (
        <div className="space-y-2 text-sm">
          {health.unmapped_project_count > 0 && (
            <Alert variant="warning" title={`${health.unmapped_project_count} unmapped project${health.unmapped_project_count === 1 ? '' : 's'}`}>
              Map every project to a Jira project so its tickets can be promoted.
            </Alert>
          )}
          {health.publishes_in_progress.length > 0 && (
            <Alert variant="info" title={`${health.publishes_in_progress.length} publish${health.publishes_in_progress.length === 1 ? '' : 'es'} in progress`}>
              {health.publishes_in_progress.map((p) => (
                <div key={p.ticket_id} className="font-mono text-xs">{p.ticket_id}</div>
              ))}
            </Alert>
          )}
          {health.publishes_unknown.length > 0 && (
            <Alert variant="warning" title={`${health.publishes_unknown.length} publish${health.publishes_unknown.length === 1 ? '' : 'es'} with an unknown result`}>
              {health.publishes_unknown.map((p) => (
                <div key={p.ticket_id} className="font-mono text-xs">{p.ticket_id}</div>
              ))}
            </Alert>
          )}
          {health.stale_links.length > 0 && (
            <Alert variant="warning" title={`${health.stale_links.length} stale link${health.stale_links.length === 1 ? '' : 's'}`}>
              {health.stale_links.map((l) => (
                <div key={l.ticket_id} className="font-mono text-xs">
                  {l.ticket_id} → {l.issue_key}
                </div>
              ))}
            </Alert>
          )}
          {health.last_push_failures.length > 0 && (
            <Alert variant="danger" title={`${health.last_push_failures.length} push failure${health.last_push_failures.length === 1 ? '' : 's'}`}>
              {health.last_push_failures.map((f) => (
                <div key={f.ticket_id} className="text-xs">
                  <span className="font-mono">{f.ticket_id}</span> → {f.issue_key ?? '—'}:{' '}
                  {f.reason ?? 'unknown error'}
                </div>
              ))}
            </Alert>
          )}
        </div>
      )}

      <div className="mt-3 flex items-center gap-1.5 text-xs text-text-tertiary">
        <Badge variant="info" label={`${health.unmapped_project_count} unmapped`} />
        <Badge variant={health.last_push_failures.length ? 'danger' : 'default'} label={`${health.last_push_failures.length} failures`} />
        <Badge variant={health.stale_links.length ? 'warning' : 'default'} label={`${health.stale_links.length} stale`} />
        <Badge variant={health.publishes_in_progress.length ? 'info' : 'default'} label={`${health.publishes_in_progress.length} in progress`} />
        <Badge variant={health.publishes_unknown.length ? 'warning' : 'default'} label={`${health.publishes_unknown.length} unknown`} />
      </div>
    </section>
  );
}
