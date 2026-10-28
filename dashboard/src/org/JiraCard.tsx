import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Combobox,
  Dialog,
  DialogFooter,
  DialogHeader,
  Input,
  Select,
  Switch,
  Table,
  Tabs,
} from '../components/ui';
import { baseField } from '../components/ui/fieldStyles';
import { CommandWell } from '../marketplace/CommandWell';
import { useProjects } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import RelativeDate from '../components/RelativeDate';
import { errorMessage } from '../utils/errorMessage';
import {
  JIRA_CUSTOM_FIELD_ID_RE,
  jiraSyncEventLabel,
  jiraSyncStateLabel,
  jiraTemplateLabel,
  jiraTemplateOptions,
} from '../utils/jira';
import { TemplatesSection } from './JiraTemplates';
import {
  ConnectionState,
  IntegrationSection,
  ProblemList,
  SectionStack,
  SiteHost,
  SummaryList,
} from './integrations/parts';
import { jiraProblems, type JiraSection } from './integrations/health';
import {
  useDeleteJiraConnection,
  useDeleteJiraMapping,
  useDetectJiraFields,
  useJiraConnection,
  useJiraHealth,
  useJiraIssueTypes,
  useJiraMappings,
  useJiraProjectSearch,
  useJiraSyncEvents,
  useJiraTemplates,
  useRotateJiraWebhookSecret,
  useSaveJiraConnection,
  useSaveJiraMapping,
  useUpdateJiraSync,
} from './useJira';
import type {
  JiraCommentSyncMode,
  JiraMapping,
  JiraStatusMap,
  JiraSyncEvent,
  JiraSyncSettingsRequest,
  JiraTemplate,
} from '../api/client';

const ENTERPRISE_TIER = 'enterprise';

export type { JiraSection };

interface JiraCardProps {
  orgId: string;
  tier: string;
  /** Controlled detail section (the org page keeps it in the URL). */
  section?: JiraSection | null;
  onSectionChange?: (section: JiraSection) => void;
}

/**
 * Org Settings → Integrations → Jira. Enterprise-only, owner/admin only
 * (the tab itself is admin-gated in OrgPage; this renders the upgrade note
 * for admins whose org is not on an Enterprise plan).
 *
 * Connected, the detail is split into sections — Overview (health first),
 * Connection, Mappings, Templates, Sync and Activity — so no single view
 * stacks every form. Not connected, only Connection and Templates apply.
 */
export function JiraCard({ orgId, tier, section, onSectionChange }: JiraCardProps) {
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

  return <EnterpriseJiraCard orgId={orgId} section={section} onSectionChange={onSectionChange} />;
}

const SECTION_LABELS: Record<JiraSection, string> = {
  overview: 'Overview',
  connection: 'Connection',
  mappings: 'Mappings',
  templates: 'Templates',
  sync: 'Sync',
  activity: 'Activity',
};
const CONNECTED_SECTIONS: JiraSection[] = ['overview', 'connection', 'mappings', 'templates', 'sync', 'activity'];
const DISCONNECTED_SECTIONS: JiraSection[] = ['connection', 'templates'];

function EnterpriseJiraCard({
  orgId,
  section,
  onSectionChange,
}: {
  orgId: string;
  section?: JiraSection | null;
  onSectionChange?: (section: JiraSection) => void;
}) {
  const connection = useJiraConnection(orgId);
  const data = connection.data;
  const connected = data?.connected === true;
  const [localSection, setLocalSection] = useState<JiraSection | null>(null);

  const sections = connected ? CONNECTED_SECTIONS : DISCONNECTED_SECTIONS;
  // Controlled (the org page keeps the section in the URL): the prop alone
  // decides, so Back/Forward land on the right section. Uncontrolled, the
  // prop is only the starting section.
  const controlled = onSectionChange !== undefined;
  const requested = controlled ? section ?? null : localSection ?? section ?? null;
  const active: JiraSection = requested && sections.includes(requested) ? requested : sections[0];
  const containerRef = useRef<HTMLDivElement>(null);
  const focusTabRef = useRef(false);
  const go = (next: JiraSection, focusTab = false) => {
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

  const content: Record<JiraSection, ReactNode> = {
    overview: <OverviewSection orgId={orgId} go={(next) => go(next, true)} />,
    connection: connected ? (
      <SectionStack>
        <ConnectionSection orgId={orgId} />
        <AgileFieldsSection orgId={orgId} />
      </SectionStack>
    ) : (
      <ConnectionSection orgId={orgId} />
    ),
    mappings: <MappingsSection orgId={orgId} />,
    templates: <TemplatesSection orgId={orgId} />,
    sync: (
      <SectionStack>
        <SyncSettingsSection orgId={orgId} />
        <WebhookSecretSection orgId={orgId} />
      </SectionStack>
    ),
    activity: <SyncEventsSection orgId={orgId} />,
  };

  return (
    <div ref={containerRef}>
      <div className="mb-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 tabIndex={-1} data-integration-heading className="text-lg font-semibold text-text-primary outline-none">Jira</h2>
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
          Promote SessionFS tickets to Jira Cloud stories, and — with two-way sync enabled — mirror
          status changes and comments between the ticket and its Jira issue.
        </p>
      </div>

      {connection.isError && (
        <Alert variant="danger" className="mb-4">
          {errorMessage(connection.error, 'Could not load the Jira connection')}
        </Alert>
      )}

      <Tabs
        aria-label="Jira sections"
        tabs={sections.map((key) => ({ key, label: SECTION_LABELS[key], content: content[key] }))}
        activeKey={active}
        onChange={(key) => go(key as JiraSection)}
      />
    </div>
  );
}

/* ── Overview: health first, then the settings at a glance ── */

const COMMENT_SYNC_LABEL: Record<string, string> = {
  off: 'Off',
  inbound: 'Inbound only',
  outbound: 'Outbound only',
  both: 'Both directions',
};

function OverviewSection({ orgId, go }: { orgId: string; go: (section: JiraSection) => void }) {
  const { data: connection } = useJiraConnection(orgId);
  const { data: mappings } = useJiraMappings(orgId);
  const { data: templates } = useJiraTemplates(orgId);
  const health = useJiraHealth(orgId);

  return (
    <SectionStack>
      <IntegrationSection title="Health" label="Jira health">
        {health.isLoading ? (
          <div className="text-sm text-text-tertiary py-2">Loading…</div>
        ) : health.isError ? (
          <Alert variant="danger">{errorMessage(health.error, 'Could not load Jira health')}</Alert>
        ) : (
          <ProblemList
            label="Jira problems"
            problems={jiraProblems(health.data!, go)}
            allGood="All good: every project is mapped and nothing failed to publish or sync."
          />
        )}
      </IntegrationSection>

      <IntegrationSection title="At a glance">
        <SummaryList
          items={[
            { label: 'Two-way status sync', value: connection?.sync_status ? 'On' : 'Off' },
            { label: 'Comment sync', value: COMMENT_SYNC_LABEL[connection?.sync_comments ?? 'off'] ?? 'Off' },
            {
              label: 'Mapped projects',
              value: mappings ? `${mappings.length}` : '—',
            },
            {
              label: 'Default template',
              value: connection?.default_template ? jiraTemplateLabel(connection.default_template, templates) : '—',
            },
            { label: 'Last inbound Jira event', value: <RelativeDate iso={health.data?.last_inbound_at} /> },
            { label: 'Last outbound sync', value: <RelativeDate iso={health.data?.last_synced_at} /> },
          ]}
        />
      </IntegrationSection>
    </SectionStack>
  );
}

/* ── Connection ── */

function ConnectionSection({ orgId }: { orgId: string }) {
  const { data } = useJiraConnection(orgId);
  const { data: templates } = useJiraTemplates(orgId);
  const save = useSaveJiraConnection(orgId);
  const disconnect = useDeleteJiraConnection(orgId);
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
      addToast('success', 'Jira disconnected');
    } catch (err: unknown) {
      setConfirmOpen(false);
      setDisconnectError(errorMessage(err, 'Could not disconnect Jira'));
    }
  }

  const showForm = !connected || editing;

  return (
    <section aria-label="Jira connection">
      <h3 className="sr-only">Connection</h3>
      {disconnectError && (
        <Alert variant="danger" className="mb-4">
          {disconnectError}
        </Alert>
      )}

      {showForm ? (
        <ConnectionForm
          // Remount with fresh values when the connection loads or changes, so
          // the stored site/email/template prefill without an effect (the token
          // is write-only and always starts blank). Sync settings live in their
          // own section and are saved with PATCH, never through this form.
          key={`${connected}:${data?.site_url ?? ''}:${data?.account_email ?? ''}:${data?.default_template ?? ''}`}
          connected={connected}
          initialSiteUrl={data?.site_url ?? ''}
          initialEmail={data?.account_email ?? ''}
          initialTemplate={data?.default_template ?? ''}
          templateOptions={jiraTemplateOptions(templates, data?.default_template)}
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
                value: data?.default_template ? jiraTemplateLabel(data.default_template, templates) : '—',
              },
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
  templateOptions: { value: string; label: string }[];
  savePending: boolean;
  onSave: ReturnType<typeof useSaveJiraConnection>['mutateAsync'];
  onSaved: () => void;
  /** Present while editing an existing connection. */
  onCancel?: () => void;
}

const SYNC_COMMENT_OPTIONS: { value: JiraCommentSyncMode; label: string }[] = [
  { value: 'off', label: 'Off' },
  { value: 'inbound', label: 'Inbound only' },
  { value: 'outbound', label: 'Outbound only' },
  { value: 'both', label: 'Both directions' },
];

function ConnectionForm({
  connected,
  initialSiteUrl,
  initialEmail,
  initialTemplate,
  templateOptions,
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
      onSaved();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not save the Jira connection'));
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
        hint="A dedicated Jira integration account is recommended so SessionFS's own changes are easy to tell apart in Jira. A personal account works too: your own transitions, edits and comments in Jira still sync back."
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
        title="Template"
        value={template}
        onValueChange={setTemplate}
        options={templateOptions}
        placeholder="Select a template"
      />
      {error && (
        <Alert variant="danger">
          {error}
        </Alert>
      )}
      <div className="flex flex-wrap items-center gap-3 pt-1">
        <Button type="submit" disabled={savePending || !formReady}>
          {savePending ? 'Saving…' : connected ? 'Save connection' : 'Connect Jira'}
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

/* ── Agile fields (sprint / epic link) ── */

const FIELD_ID_ERROR = 'Use a Jira custom field id like customfield_10020.';

/** Client copy of the server's field-id check: blank (clears) or customfield_<n>. */
function fieldIdError(value: string): string | undefined {
  const v = value.trim();
  return v === '' || JIRA_CUSTOM_FIELD_ID_RE.test(v) ? undefined : FIELD_ID_ERROR;
}

/**
 * The sprint and epic-link custom fields, saved through the sync PATCH so the
 * API token is never asked for again. Only a changed field is sent (a cleared
 * one as null), so an untouched override is never rewritten.
 */
function AgileFieldsSection({ orgId }: { orgId: string }) {
  const { data } = useJiraConnection(orgId);
  const stored = { sprint: data?.sprint_field_id ?? '', epic: data?.epic_link_field_id ?? '' };
  const site = data?.site_url ?? '';
  // Notice a reconnect to a different site while this section is open: ids
  // kept from the old site may not exist on the new one. (Adjusting state
  // while rendering, the documented way to react to a changed value.)
  const [seenSite, setSeenSite] = useState(site);
  const [siteChanged, setSiteChanged] = useState(false);
  if (site !== seenSite) {
    setSeenSite(site);
    if (seenSite && site) setSiteChanged(true);
  }
  const showSiteNotice = siteChanged && (stored.sprint !== '' || stored.epic !== '');
  return (
    <IntegrationSection
      title="Agile fields"
      label="Jira agile fields"
      description="Custom fields SessionFS uses to put issues in a sprint and under an epic. They are detected when the site is first connected; Detect fields asks the site again, and you can set one by hand if detection picked the wrong field."
    >
      {showSiteNotice && (
        <Alert variant="warning" title="Site changed — run Detect fields" className="mb-3 max-w-md">
          These field ids were set for the previous Jira site and may not exist on{' '}
          <span className="font-mono">{site}</span>. Run Detect fields and save what it finds.
        </Alert>
      )}
      {/* Remount with the stored values after every save or refetch, and on a
          reconnect (new site or re-verified), so a detection result or edit
          from the old site can never be saved onto the new one. */}
      <AgileFieldsForm
        key={`${site}:${data?.last_verified_at ?? ''}:${stored.sprint}:${stored.epic}`}
        orgId={orgId}
        stored={stored}
        onConfirmed={() => setSiteChanged(false)}
      />
    </IntegrationSection>
  );
}

function AgileFieldsForm({
  orgId,
  stored,
  onConfirmed,
}: {
  orgId: string;
  stored: { sprint: string; epic: string };
  /** Detection ran or the fields were saved for the current site. */
  onConfirmed: () => void;
}) {
  const save = useUpdateJiraSync(orgId);
  const detect = useDetectJiraFields(orgId);
  const { addToast } = useToast();
  const [sprint, setSprint] = useState(stored.sprint);
  const [epic, setEpic] = useState(stored.epic);
  const [showErrors, setShowErrors] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [detected, setDetected] = useState<string | null>(null);
  const [detectError, setDetectError] = useState<string | null>(null);
  // A reconnect remounts this form; a detection still in flight for the old
  // site must not then mark the new site as checked.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const sprintError = fieldIdError(sprint);
  const epicError = fieldIdError(epic);
  const changes: { sprint_field_id?: string | null; epic_link_field_id?: string | null } = {};
  if (sprint.trim() !== stored.sprint) changes.sprint_field_id = sprint.trim() || null;
  if (epic.trim() !== stored.epic) changes.epic_link_field_id = epic.trim() || null;
  const dirty = Object.keys(changes).length > 0;

  // Detection fills the fields for review rather than saving straight away:
  // an admin may have overridden a field on purpose, and a wrong guess would
  // silently undo that. Saving is one more click.
  async function handleDetect() {
    setDetectError(null);
    setDetected(null);
    setSaveError(null);
    try {
      const found = await detect.mutateAsync();
      if (!alive.current) return;
      const nextSprint = found.sprint_field_id ?? '';
      const nextEpic = found.epic_link_field_id ?? '';
      setSprint(nextSprint);
      setEpic(nextEpic);
      onConfirmed();
      const sprintText = found.sprint_field_id ? `sprint field ${found.sprint_field_id}` : 'no sprint field';
      const epicText = found.epic_link_field_id ? `epic-link field ${found.epic_link_field_id}` : 'no epic-link field';
      const same = nextSprint === stored.sprint && nextEpic === stored.epic;
      setDetected(
        same
          ? `Found ${sprintText} and ${epicText}, which is what is saved.`
          : `Found ${sprintText} and ${epicText}. Review them and save to use them.`,
      );
    } catch (err: unknown) {
      setDetectError(errorMessage(err, 'Could not detect the Jira fields'));
    }
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setSaveError(null);
    if (sprintError || epicError) {
      setShowErrors(true);
      return;
    }
    if (!dirty) return;
    try {
      await save.mutateAsync(changes);
      onConfirmed();
      addToast('success', 'Jira agile fields saved');
    } catch (err: unknown) {
      setSaveError(errorMessage(err, 'Could not save the Jira agile fields'));
    }
  }

  return (
    <form onSubmit={handleSave} className="space-y-3 max-w-md" autoComplete="off" noValidate>
      <Input
        title="Sprint field ID"
        value={sprint}
        onChange={(e) => setSprint(e.target.value)}
        onBlur={() => setShowErrors(true)}
        placeholder="customfield_10020"
        spellCheck={false}
        className="font-mono"
        error={showErrors ? sprintError : undefined}
        hint={stored.sprint ? 'Clear it to stop putting issues in sprints.' : 'Not set: publishing can’t put an issue in a sprint.'}
      />
      <Input
        title="Epic-link field ID"
        value={epic}
        onChange={(e) => setEpic(e.target.value)}
        onBlur={() => setShowErrors(true)}
        placeholder="customfield_10014"
        spellCheck={false}
        className="font-mono"
        error={showErrors ? epicError : undefined}
        hint="Older company-managed projects only. Jira Cloud usually has none and sets the parent directly."
      />
      <p role="status" className="text-xs text-text-secondary empty:hidden">
        {detect.isPending ? 'Asking Jira which fields this site has…' : detected ?? ''}
      </p>
      {detectError && <Alert variant="danger">{detectError}</Alert>}
      {saveError && <Alert variant="danger">{saveError}</Alert>}
      <div className="flex flex-wrap items-center gap-3 pt-1">
        <Button type="submit" disabled={!dirty} loading={save.isPending}>
          Save fields
        </Button>
        <Button type="button" variant="secondary" loading={detect.isPending} onClick={() => void handleDetect()}>
          Detect fields
        </Button>
        {dirty && (
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setSprint(stored.sprint);
              setEpic(stored.epic);
              setShowErrors(false);
              setDetected(null);
            }}
          >
            Reset
          </Button>
        )}
      </div>
    </form>
  );
}

/* ── Two-way sync settings ── */

/** Sentinel the server uses for an unmapped status / category. */
const NONE = 'none';

const TRANSITION_TARGETS = ['open', 'in_progress', 'blocked', 'review', 'done', 'cancelled', 'closed'] as const;
const TRANSITION_LABELS: Record<(typeof TRANSITION_TARGETS)[number], string> = {
  open: 'Open',
  in_progress: 'In progress',
  blocked: 'Blocked',
  review: 'Review',
  done: 'Done',
  cancelled: 'Cancelled',
  closed: 'Closed',
};
const CATEGORY_KEYS = ['new', 'indeterminate', 'done'] as const;
const CATEGORY_LABELS: Record<(typeof CATEGORY_KEYS)[number], string> = {
  new: 'Jira “To Do” category',
  indeterminate: 'Jira “In Progress” category',
  done: 'Jira “Done” category',
};
const CATEGORY_ACTION_OPTIONS = [
  { value: NONE, label: 'Not synced' },
  { value: 'resolve', label: 'Resolve the ticket' },
  { value: 'start', label: 'Start the ticket' },
  { value: 'reopen', label: 'Reopen the ticket' },
];

/** The server's default status map (kept in step with the backend). */
function defaultStatusMap(): JiraStatusMap {
  return {
    transitions: {
      open: 'To Do',
      in_progress: 'In Progress',
      blocked: NONE,
      review: NONE,
      done: 'Done',
      cancelled: NONE,
      closed: 'Done',
    },
    categories: { new: NONE, indeterminate: NONE, done: 'resolve' },
  };
}

/** Fill any key the stored map omits with the server default. */
function withDefaults(map: JiraStatusMap | undefined): JiraStatusMap {
  const base = defaultStatusMap();
  return {
    transitions: { ...base.transitions, ...(map?.transitions ?? {}) },
    categories: { ...base.categories, ...(map?.categories ?? {}) },
  };
}

/** Editor value → wire value: blank (or "none" in any case) means unmapped. */
function normalizeStatusMap(map: JiraStatusMap): JiraStatusMap {
  const transitions: Record<string, string> = {};
  for (const [k, v] of Object.entries(map.transitions)) {
    const trimmed = (v ?? '').trim();
    transitions[k] = trimmed === '' || trimmed.toLowerCase() === NONE ? NONE : trimmed;
  }
  const categories: Record<string, string> = {};
  for (const [k, v] of Object.entries(map.categories)) {
    categories[k] = v || NONE;
  }
  return { transitions, categories };
}

function sameStatusMap(a: JiraStatusMap, b: JiraStatusMap): boolean {
  const keys = (r: Record<string, string>) => Object.keys(r).sort();
  const eq = (x: Record<string, string>, y: Record<string, string>) =>
    keys(x).join() === keys(y).join() && keys(x).every((k) => x[k] === y[k]);
  return eq(a.transitions, b.transitions) && eq(a.categories, b.categories);
}

function SyncSettingsSection({ orgId }: { orgId: string }) {
  const { data } = useJiraConnection(orgId);
  const initialSyncStatus = data?.sync_status ?? false;
  const initialSyncComments = data?.sync_comments ?? 'off';
  const initialStatusMap = normalizeStatusMap(withDefaults(data?.status_map));

  return (
    <SyncSettingsForm
      // Remount when the stored settings change (e.g. after a save or a
      // refetch), so the form always starts from the server's values.
      key={JSON.stringify([initialSyncStatus, initialSyncComments, initialStatusMap])}
      orgId={orgId}
      initialSyncStatus={initialSyncStatus}
      initialSyncComments={initialSyncComments}
      initialStatusMap={initialStatusMap}
    />
  );
}

function SyncSettingsForm({
  orgId,
  initialSyncStatus,
  initialSyncComments,
  initialStatusMap,
}: {
  orgId: string;
  initialSyncStatus: boolean;
  initialSyncComments: JiraCommentSyncMode;
  initialStatusMap: JiraStatusMap;
}) {
  const update = useUpdateJiraSync(orgId);
  const { addToast } = useToast();
  const [syncStatus, setSyncStatus] = useState(initialSyncStatus);
  const [syncComments, setSyncComments] = useState<JiraCommentSyncMode>(initialSyncComments);
  const [statusMap, setStatusMap] = useState<JiraStatusMap>(initialStatusMap);
  const [error, setError] = useState<string | null>(null);

  // Only the fields that changed go into the PATCH; the server leaves the
  // rest as stored.
  const body: JiraSyncSettingsRequest = {};
  if (syncStatus !== initialSyncStatus) body.sync_status = syncStatus;
  if (syncComments !== initialSyncComments) body.sync_comments = syncComments;
  const normalizedMap = normalizeStatusMap(statusMap);
  if (!sameStatusMap(normalizedMap, initialStatusMap)) body.status_map = normalizedMap;
  const dirty = Object.keys(body).length > 0;

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    if (!dirty) return;
    setError(null);
    try {
      await update.mutateAsync(body);
      addToast('success', 'Jira sync settings saved');
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not save the sync settings'));
    }
  }

  function handleReset() {
    setSyncStatus(initialSyncStatus);
    setSyncComments(initialSyncComments);
    setStatusMap(initialStatusMap);
    setError(null);
  }

  return (
    <section className="pb-5" aria-labelledby="jira-sync-heading">
      <h3 id="jira-sync-heading" className="text-sm font-semibold text-text-primary">
        Two-way sync
      </h3>
      <p className="mt-0.5 mb-4 text-sm text-text-tertiary">
        Saved separately from the connection, so changing these never asks for the API token.
      </p>
      <form onSubmit={handleSave} className="space-y-5" autoComplete="off">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="flex items-start justify-between gap-3 rounded-lg border border-border bg-surface px-4 py-3">
            <div>
              <span className="text-sm font-medium text-text-primary">Two-way status sync</span>
              <p className="text-xs text-text-tertiary">
                Apply mapped Jira status changes to linked tickets, and push ticket status changes to
                Jira.
              </p>
            </div>
            <Switch checked={syncStatus} onChange={setSyncStatus} aria-label="Two-way status sync" />
          </div>
          <Select
            title="Comment sync"
            value={syncComments}
            onValueChange={(v) => setSyncComments(v as JiraCommentSyncMode)}
            options={SYNC_COMMENT_OPTIONS}
            hint="Outbound sends a comment when an editor ticks “Send to Jira”, and forwards a trusted reviewer’s VERIFIED-CLEAN verdict automatically."
          />
        </div>

        <StatusMapEditor value={statusMap} onChange={setStatusMap} syncOff={!syncStatus} />

        {error && <Alert variant="danger">{error}</Alert>}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="submit"
            variant={dirty ? 'primary' : 'secondary'}
            disabled={!dirty || update.isPending}
            loading={update.isPending}
          >
            {update.isPending ? 'Saving…' : 'Save sync settings'}
          </Button>
          {dirty && (
            <Button type="button" variant="ghost" onClick={handleReset}>
              Discard changes
            </Button>
          )}
        </div>
      </form>
    </section>
  );
}

/** Short column labels for the inbound table (the selects keep their full names). */
const CATEGORY_SHORT: Record<(typeof CATEGORY_KEYS)[number], string> = {
  new: 'To Do',
  indeterminate: 'In Progress',
  done: 'Done',
};

function StatusMapEditor({
  value,
  onChange,
  syncOff,
}: {
  value: JiraStatusMap;
  onChange: (next: JiraStatusMap) => void;
  /** Two-way status sync is off: the map stays editable so it can be prepared,
   *  and is applied once sync is turned on. */
  syncOff: boolean;
}) {
  const setTransition = (target: string, name: string) =>
    onChange({ ...value, transitions: { ...value.transitions, [target]: name } });
  const setCategory = (category: string, action: string) =>
    onChange({ ...value, categories: { ...value.categories, [category]: action } });
  const shown = (v: string | undefined) => (!v || v === NONE ? '' : v);

  return (
    <fieldset className="space-y-3" aria-describedby={syncOff ? 'jira-status-map-off' : undefined}>
      <legend className="text-sm font-medium text-text-primary">Status map</legend>
      {syncOff && (
        <p id="jira-status-map-off" className="flex items-start gap-1.5 text-xs text-text-tertiary">
          <span aria-hidden="true" className="mt-1 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-info" />
          Two-way status sync is off. Changes to the map are saved now and used once sync is on.
        </p>
      )}
      <div className="grid grid-cols-1 gap-x-8 gap-y-5 lg:grid-cols-[3fr_2fr]">
        <div>
          <div className="mb-1 grid grid-cols-[minmax(6.5rem,8rem)_1fr] gap-3 px-1 text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary">
            <span>SessionFS status</span>
            <span>Jira transition</span>
          </div>
          <div className="divide-y divide-border border-y border-border">
            {TRANSITION_TARGETS.map((target) => {
              const id = `jira-transition-${target}`;
              return (
                <div key={target} className="grid grid-cols-[minmax(6.5rem,8rem)_1fr] items-center gap-3 px-1 py-1.5">
                  <label htmlFor={id} className="text-sm text-text-secondary">
                    {TRANSITION_LABELS[target]}
                  </label>
                  <input
                    id={id}
                    className={`${baseField} py-1.5`}
                    value={shown(value.transitions[target])}
                    onChange={(e) => setTransition(target, e.target.value)}
                    placeholder="Not synced"
                  />
                </div>
              );
            })}
          </div>
          <p className="mt-2 text-xs text-text-tertiary">
            Outbound: the Jira transition that moves the issue when the ticket reaches each status.
            Leave a field blank to not sync that status.
          </p>
        </div>
        <div>
          <div className="mb-1 grid grid-cols-[minmax(6.5rem,7rem)_1fr] gap-3 px-1 text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary">
            <span>Jira category</span>
            <span>Ticket action</span>
          </div>
          <div className="divide-y divide-border border-y border-border">
            {CATEGORY_KEYS.map((category) => (
              <div key={category} className="grid grid-cols-[minmax(6.5rem,7rem)_1fr] items-center gap-3 px-1 py-1.5">
                <span className="text-sm text-text-secondary" aria-hidden="true">
                  {CATEGORY_SHORT[category]}
                </span>
                <Select
                  aria-label={CATEGORY_LABELS[category]}
                  value={value.categories[category] || NONE}
                  onValueChange={(action) => setCategory(category, action)}
                  options={CATEGORY_ACTION_OPTIONS}
                  className="py-1.5"
                />
              </div>
            ))}
          </div>
          <p className="mt-2 text-xs text-text-tertiary">
            Inbound: what happens to the ticket when its Jira issue enters each status category.
          </p>
        </div>
      </div>
    </fieldset>
  );
}

/* ── Webhook (inbound sync) ── */

function WebhookSecretSection({ orgId }: { orgId: string }) {
  const { data } = useJiraConnection(orgId);
  const rotate = useRotateJiraWebhookSecret(orgId);
  const { addToast } = useToast();
  const [secret, setSecret] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const hasSecret = data?.has_webhook_secret === true;

  async function handleRotate() {
    setError(null);
    try {
      const result = await rotate.mutateAsync();
      setSecret(result.webhook_secret);
      addToast('success', hasSecret ? 'Webhook secret rotated' : 'Webhook secret generated');
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not generate the webhook secret'));
    }
  }

  return (
    <IntegrationSection
      title="Webhook"
      label="Jira webhook secret"
      description={
        <>
          Authenticates inbound Jira webhooks. The secret is stored encrypted so the server can verify
          each webhook's signature, and only its last four characters are ever displayed.
        </>
      }
      className="pb-0"
    >
      <div className="space-y-3">
        {data?.webhook_url && (
          <div>
            <p className="mb-1 text-xs font-medium text-text-secondary">Webhook URL</p>
            <CommandWell command={data.webhook_url} label="Jira webhook URL" copiedMessage="Webhook URL copied" />
          </div>
        )}
        {secret ? (
          <div>
            <p className="mb-1 text-xs font-medium text-text-secondary">Secret</p>
            <CommandWell command={secret} label="Jira webhook secret" copiedMessage="Webhook secret copied" />
            <p className="mt-1 text-xs text-warning">
              Copy this secret into Jira's webhook configuration now. It will not be shown again.
            </p>
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="text-text-secondary">
            {hasSecret ? (
              <>
                Stored secret ends in{' '}
                <span className="font-mono text-xs" title={data!.webhook_secret_last4 ?? ''}>
                  {data!.webhook_secret_last4 ?? ''}
                </span>
              </>
            ) : (
              <span className="text-text-tertiary">No webhook secret yet</span>
            )}
          </span>
          <Button size="sm" variant="secondary" loading={rotate.isPending} onClick={handleRotate}>
            {hasSecret ? 'Rotate secret' : 'Generate secret'}
          </Button>
        </div>
        {error && <Alert variant="danger">{error}</Alert>}
      </div>
    </IntegrationSection>
  );
}

/* ── Activity: the two-way sync ledger ── */

function SyncStateBadge({ event }: { event: JiraSyncEvent }) {
  const state = jiraSyncStateLabel(event);
  if (!state) return null;
  const title =
    event.state === 'pending' && event.next_attempt_at
      ? `Next attempt ${new Date(event.next_attempt_at).toLocaleString()}`
      : undefined;
  return (
    <span title={title}>
      <Badge variant={state.variant} label={state.label} tint />
    </span>
  );
}

function DirectionIcon({ direction }: { direction: 'inbound' | 'outbound' }) {
  // Arrow into SessionFS (inbound) or out to Jira (outbound).
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="shrink-0 text-text-tertiary">
      {direction === 'inbound' ? (
        <>
          <line x1="17" y1="7" x2="7" y2="17" />
          <polyline points="17 17 7 17 7 7" />
        </>
      ) : (
        <>
          <line x1="7" y1="17" x2="17" y2="7" />
          <polyline points="7 7 17 7 17 17" />
        </>
      )}
    </svg>
  );
}

function SyncEventsSection({ orgId }: { orgId: string }) {
  const { data, isLoading, isError } = useJiraSyncEvents(orgId, { limit: 10 });
  const events = data?.events ?? [];

  return (
    <IntegrationSection
      title="Recent sync events"
      label="Recent Jira sync events"
      description="The latest status and comment changes mirrored between tickets and Jira issues."
    >
      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : isError ? (
        <div className="text-sm text-text-tertiary py-2">Could not load sync events.</div>
      ) : events.length === 0 ? (
        <div className="text-sm text-text-tertiary py-2">
          No sync events yet. Enable two-way sync on the connection and status changes or comments
          will appear here.
        </div>
      ) : (
        <ul className="divide-y divide-border border-y border-border">
          {events.map((e) => (
            <li key={e.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2.5 text-sm">
              <span className="mt-0.5" title={e.direction === 'inbound' ? 'Inbound from Jira' : 'Outbound to Jira'}>
                <DirectionIcon direction={e.direction} />
                <span className="sr-only">{e.direction}</span>
              </span>
              <div className="min-w-0 flex-[1_1_12rem]">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-text-primary">{jiraSyncEventLabel(e)}</span>
                  {e.issue_key && <span className="font-mono text-xs text-text-tertiary">{e.issue_key}</span>}
                  <SyncStateBadge event={e} />
                </div>
                {e.error && (e.state === 'exhausted' || e.state === 'pending' || e.applied === false) && (
                  <p className="mt-0.5 break-all text-xs text-text-tertiary">{e.error}</p>
                )}
              </div>
              <span className="shrink-0 text-xs text-text-tertiary">
                <RelativeDate iso={e.created_at} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </IntegrationSection>
  );
}

/* ── Mappings ── */

type MappingDialogState =
  | { mode: 'new' }
  | { mode: 'edit'; mapping: JiraMapping }
  | { mode: 'remove'; mapping: JiraMapping };

function MappingsSection({ orgId }: { orgId: string }) {
  const { data: mappings, isLoading } = useJiraMappings(orgId);
  const { data: projects, isLoading: orgProjectsLoading } = useProjects();
  const { data: templates } = useJiraTemplates(orgId);
  const [dialog, setDialog] = useState<MappingDialogState | null>(null);
  const { addToast } = useToast();

  // `GET /api/v1/projects/` lists every project the caller can read; only the
  // ones owned by this org can be mapped. (Before GitHub #103 the response
  // carried no org_id, so this filter dropped everything.)
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
      label="Jira project mappings"
      description="Which Jira project, issue type and template each SessionFS project promotes to."
      actions={
        <Button size="sm" variant="secondary" onClick={() => setDialog({ mode: 'new' })}>
          Add mapping
        </Button>
      }
    >
      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : (
        <Table<JiraMapping>
          label="Jira project mappings table"
          columns={[
            {
              key: 'project',
              header: 'SessionFS project',
              render: (m) => <span className="text-text-primary">{projectName(m.project_id)}</span>,
            },
            { key: 'key', header: 'Jira project', render: (m) => <span className="font-mono">{m.jira_project_key}</span> },
            { key: 'issue_type', header: 'Issue type', render: (m) => m.issue_type },
            {
              key: 'template',
              header: 'Template',
              render: (m) => (m.template ? jiraTemplateLabel(m.template, templates) : <span className="text-text-tertiary">Org default</span>),
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
          orgProjectsLoading={orgProjectsLoading}
          mappedProjectIds={mappedProjectIds}
          templates={templates}
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
          templates={templates}
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
  /** Built-ins + customs (incl. archived) from the templates list. */
  templates?: JiraTemplate[];
  initial?: JiraMapping;
  onClose: () => void;
  onSaved: (projectId: string) => void;
}

function MappingEditorDialog({
  orgId,
  orgProjects,
  orgProjectsLoading,
  mappedProjectIds,
  templates,
  initial,
  onClose,
  onSaved,
}: MappingEditorDialogProps) {
  // The mapping's saved override stays selectable even if it has since been
  // archived (labelled), so editing other fields never switches it.
  const templateOptions = useMemo(
    () => [{ value: '', label: 'Org default' }, ...jiraTemplateOptions(templates, initial?.template)],
    [templates, initial?.template],
  );
  const save = useSaveJiraMapping(orgId);
  const [jiraQuery, setJiraQuery] = useState('');
  const jiraSearch = useJiraProjectSearch(orgId, jiraQuery);

  const isEdit = initial != null;
  const [projectId, setProjectId] = useState(initial?.project_id ?? '');
  const [jiraKey, setJiraKey] = useState(initial?.jira_project_key ?? '');
  // The picked project's "KEY · Name" label, kept so the trigger still reads
  // well after a later search returns a page without it.
  const [jiraLabel, setJiraLabel] = useState(initial?.jira_project_key ?? '');
  const [issueType, setIssueType] = useState(initial?.issue_type ?? '');
  const [template, setTemplate] = useState<string>(initial?.template ?? '');
  const [doneStatus, setDoneStatus] = useState(initial?.done_status_name ?? '');
  const [error, setError] = useState<string | null>(null);

  const issueTypes = useJiraIssueTypes(orgId, jiraKey || null);
  // A new mapping offers only unmapped projects (saving an already-mapped
  // one would silently replace its mapping); an edit shows its own project.
  const projectOptions = orgProjects
    .filter((p) => (initial ? p.id === initial.project_id : !mappedProjectIds.has(p.id)))
    .map((p) => ({ value: p.id, label: p.name }));
  const projectEmptyMessage = orgProjectsLoading
    ? 'Loading projects…'
    : orgProjects.length === 0
      ? 'No projects in this organization yet — create one first'
      : 'All projects in this organization are already mapped';
  const jiraPage = jiraSearch.data;
  const jiraOptions = (jiraPage?.projects ?? []).map((p) => ({ value: p.key, label: `${p.key} · ${p.name}` }));
  const jiraFooter = jiraPage?.has_more
    ? jiraSearch.debouncedQuery
      ? `Showing the first ${jiraPage.projects.length} matches — keep typing to narrow them`
      : `Showing the first ${jiraPage.projects.length} projects — type to search`
    : null;
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
          title="Jira project"
          value={jiraKey}
          onValueChange={(key, option) => {
            setJiraKey(key);
            setJiraLabel(option.label);
          }}
          options={jiraOptions}
          selectedLabel={jiraLabel}
          placeholder="Select a Jira project"
          searchPlaceholder="Search by key or name"
          onSearchChange={setJiraQuery}
          loading={jiraSearch.searching && !jiraSearch.isError}
          // Until the debounced search answers, the list still shows the
          // previous query's page; never let Enter or a click pick from it.
          pending={jiraSearch.searching && !jiraSearch.isError}
          emptyMessage={
            jiraSearch.isError
              ? errorMessage(jiraSearch.error, 'Could not load Jira projects')
              : 'No Jira projects are visible to the connected account'
          }
          noMatchesMessage={(q) =>
            jiraSearch.isError
              ? errorMessage(jiraSearch.error, 'Could not search Jira projects')
              : `No Jira projects match “${q}”`
          }
          footer={jiraFooter}
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
          options={templateOptions}
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
