import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  type JiraLink,
  type JiraLinkParent,
  type JiraLinkSprint,
  type JiraPreview,
  type JiraSection,
  type JiraSprint,
  type JiraSprintList,
  type Ticket,
} from '../../api/client';
import {
  Alert,
  Badge,
  Button,
  Combobox,
  Dialog,
  DialogFooter,
  DialogHeader,
  Dropdown,
  Input,
  Select,
  Textarea,
  type ComboboxOption,
} from '../../components/ui';
import {
  useJiraLink,
  useJiraPreview,
  useJiraSprints,
  useLinkJiraIssue,
  usePullJira,
  usePublishJira,
  useTicketJiraTemplates,
  useUnlinkJira,
} from '../../hooks/useJiraTickets';
import { useTicketPreview } from '../../hooks/useTickets';
import { useToast } from '../../hooks/useToast';
import { errorMessage } from '../../utils/errorMessage';
import {
  defaultJiraTemplateChoice,
  jiraConnectionSettingsHref,
  jiraErrorCode,
  jiraPublishWarningMessage,
  jiraSiblingIssueUrl,
  jiraSprintDateLabel,
  jiraSprintName,
  jiraSprintStateLabel,
  jiraSprintStateVariant,
  parseJiraIssueInput,
  sortJiraSprints,
  urlHost,
} from '../../utils/jira';
import { safeHttpUrl } from '../../utils/url';
import { useTicketViewer } from './viewer';

/* ── Ticket-detail Jira surface (Enterprise, org-scoped projects) ──
 *
 * The button appears for any org-scoped project (the connection GET is
 * admin-only, so a member learns connection/mapping state from the preview
 * POST's error codes inside the dialog). The badge reflects the ticket's
 * embedded link summary, with the live status refreshed via GET /jira. */

const MAX_SUMMARY = 255;

/**
 * The provenance note shown above the draft: which template this publish
 * resolves to, and — on a re-publish — whether the link's pinned version has
 * since moved. Built-in templates carry no version, so a bump only shows for
 * custom templates.
 */
function templateNote(preview: JiraPreview): string | null {
  const current = preview.template;
  const existing = preview.existing;
  if (!existing) {
    return current.version != null ? `Using ${current.name} v${current.version}.` : `Using ${current.name}.`;
  }
  const publishedSlug = existing.template_slug;
  if (!publishedSlug) return null; // a link predating template pinning
  if (publishedSlug !== current.slug) {
    return `Published with ${publishedSlug}; current is ${current.name}.`;
  }
  if (
    existing.template_version != null &&
    current.version != null &&
    existing.template_version !== current.version
  ) {
    return `Published with ${current.name} v${existing.template_version}; current is v${current.version}.`;
  }
  return null;
}

const TEMPLATE_CHANGED_MESSAGE =
  'The story template changed since this draft was built. The draft has been rebuilt with the current template; review it and publish again.';

function friendlyError(err: unknown): string {
  switch (jiraErrorCode(err)) {
    case 'jira_project_unmapped':
      return 'Ask your admin to add a Jira mapping for this project.';
    case 'jira_not_connected':
      return 'This organization has not connected Jira yet.';
    case 'upgrade_required':
      return 'Jira promotion requires the Enterprise plan.';
    case 'insufficient_role':
    case 'service_key_not_allowed':
      return "You don't have permission to promote this ticket.";
    case 'publish_in_progress':
      return 'Another publish for this ticket is already in progress.';
    case 'jira_publish_unknown':
      return 'A previous publish timed out and the Jira issue is not confirmed; publish again to reconcile.';
    case 'jira_link_stale':
      return "This ticket's Jira link belongs to another organization or site; a project admin can unlink it first.";
    case 'publish_superseded':
      return 'This publish was superseded by a newer one; no link changed.';
    case 'link_changed':
      return "This ticket's Jira link changed while publishing; no update was applied.";
    case 'content_too_large':
    case 'content_too_large_for_jira':
      return 'Some content is too large to publish to Jira Cloud.';
    case 'jira_connection_failed':
      return 'Jira rejected the stored connection; an admin should re-save the connection.';
    case 'jira_upstream_error':
    case 'jira_timeout':
      return 'Jira is unreachable right now; try again shortly.';
    case 'template_changed':
      return TEMPLATE_CHANGED_MESSAGE;
    case 'template_archived':
      return 'That template was archived; pick another one.';
    case 'jira_sprint_field_unknown':
      return 'This Jira connection has no sprint field yet; publish without a sprint, or ask an org admin to detect fields in the Jira connection settings.';
    default:
      return errorMessage(err, 'Could not load the Jira draft');
  }
}

const INVALID_KEY_MESSAGE = 'Enter a Jira issue key like PROJ-123, or paste the issue’s link.';

/**
 * Where a link failure is shown: problems with the key itself sit under the
 * field (tied to it with aria-describedby); everything else — Jira being
 * down, permissions, connection state — is a form-level alert.
 */
function linkErrorMessage(err: unknown): { field: boolean; message: string } {
  const code = jiraErrorCode(err);
  switch (code) {
    case 'invalid_issue_key':
      return { field: true, message: INVALID_KEY_MESSAGE };
    case 'jira_issue_not_found':
      return { field: true, message: 'No issue with that key, or the Jira connection can’t see it.' };
    case 'jira_issue_linked_elsewhere':
      return { field: true, message: 'That issue is already linked to another ticket.' };
    case 'jira_project_not_mapped':
      // The server names the issue's project and the mapped one; say that.
      return {
        field: true,
        message: errorMessage(err, 'That issue’s Jira project isn’t mapped to this project.'),
      };
    case 'jira_timeout':
    case 'jira_upstream_error':
      return { field: false, message: 'Jira is unreachable right now; try again shortly.' };
  }
  if (err instanceof ApiError) {
    // A FastAPI body-validation 422 (the key failed the server's shape check).
    if (err.status === 422 && err.details && 'errors' in err.details) {
      return { field: true, message: INVALID_KEY_MESSAGE };
    }
    if (err.status === 403 && (!code || /^\d+$/.test(code) || code === 'project_edit_forbidden')) {
      return { field: false, message: 'You don’t have permission to link this ticket.' };
    }
  }
  const message = friendlyError(err);
  return {
    field: false,
    message: message === 'Could not load the Jira draft' ? 'Could not link the Jira issue.' : message,
  };
}

interface JiraTicketSectionProps {
  projectId: string;
  ticket: Ticket;
  orgId: string;
  isProjectAdmin: boolean;
  isEnterprise: boolean;
  /** Project editor; only editors may link an existing issue. Defaults true. */
  canEdit?: boolean;
  /** Open another ticket of this project in place (the Jira parent's ticket). */
  onNavigateTicket?: (ticketId: string) => void;
}

export function JiraTicketSection({
  projectId,
  ticket,
  orgId,
  isProjectAdmin,
  isEnterprise,
  canEdit = true,
  onNavigateTicket,
}: JiraTicketSectionProps) {
  // 'overwrite' opens the publish dialog already committed to replacing an
  // issue that was linked from Jira (it asks before sending either way).
  const [promoteMode, setPromoteMode] = useState<'publish' | 'overwrite' | null>(null);
  const [confirmUnlink, setConfirmUnlink] = useState(false);
  // 'replace' = chosen from the sync menu; 'update' = the plain button.
  const [pullMode, setPullMode] = useState<'replace' | 'update' | null>(null);
  const [linkOpen, setLinkOpen] = useState(false);
  const linkToggleRef = useRef<HTMLButtonElement>(null);
  const syncTriggerRef = useRef<HTMLButtonElement>(null);
  const groupRef = useRef<HTMLDivElement>(null);
  const linkFormId = useId();

  const linked = ticket.jira ?? null;
  // Refresh the live status only when there is a confirmed link; in-progress
  // and unknown tickets have no issue key to GET yet.
  const linkQuery = useJiraLink(projectId, ticket.id, !!linked);
  const link = linkQuery.data ?? null;

  const showPromote = !!orgId && isEnterprise;
  const showUpgradeHint = !!orgId && !isEnterprise;
  const showUnlink = isProjectAdmin && !!linked;
  // Linked from Jira and never published from here: neither side has
  // overwritten the other yet, so an editor picks which one wins.
  const attached = !!linked && link?.attached_not_published === true;
  // Which controls a linked ticket gets depends on the live link, so hold
  // them back until it has loaded (or failed) rather than swap them later.
  const linkSettled = !linked || !!link || linkQuery.isError === true;
  // Taking the Jira text into the ticket edits it, which the server allows
  // only for the ticket's creator or a project admin (authorize_ticket_edit).
  // Without a known viewer id, offer it and let the server's 403 decide.
  const viewer = useTicketViewer();
  const canPull =
    canEdit &&
    (isProjectAdmin || !viewer.userId || ticket.created_by_user_id === viewer.userId);
  const canSync = showPromote && canEdit && attached;
  // Both directions: a menu. Only the Jira side: a single control.
  const showSyncChoice = canSync && canPull;
  const showOverwriteOnly = canSync && !canPull;
  // On a published link, taking the Jira text is a plain secondary action;
  // on an attached one it lives in the sync menu instead.
  const showUpdateFromJira = showPromote && canPull && !!linked && !!link && !attached;
  // Linking an existing issue is the alternative to a first publish: only on
  // an unlinked ticket with no publish in flight or awaiting reconciliation.
  const showLink =
    showPromote && canEdit && !linked && !ticket.jira_publish_in_progress && !ticket.jira_publish_unknown;
  const linkFormOpen = showLink && linkOpen;

  function closeLinkForm() {
    setLinkOpen(false);
    // Hand focus back to the control that opened the form.
    requestAnimationFrame(() => linkToggleRef.current?.focus());
  }

  function handleLinked() {
    setLinkOpen(false);
    // The toggle disappears with the linked state; keep focus in this group
    // (now showing the linked issue) instead of dropping it on the page.
    requestAnimationFrame(() => groupRef.current?.focus());
  }

  if (!showPromote && !showUpgradeHint && !linked && !ticket.jira_publish_in_progress && !ticket.jira_publish_unknown) {
    return null;
  }

  // Compact: sits in the ticket header next to the status and assignee.
  return (
    <div>
      <div
        ref={groupRef}
        tabIndex={-1}
        role="group"
        aria-label="Jira issue"
        className="flex flex-wrap items-center gap-2 outline-none"
      >
        {linked && (
          <LinkedIssueBadge
            issueKey={linked.issue_key}
            issueUrl={linked.issue_url}
            status={link?.last_known_status ?? linked.last_known_status ?? 'linked'}
          />
        )}

        {ticket.jira_publish_in_progress && (
          <Badge variant="default" label="publishing…" />
        )}
        {ticket.jira_publish_unknown && (
          <Badge variant="warning" label="publish result unknown" />
        )}

        {showPromote && linkSettled && !attached && (
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setPromoteMode('publish')}
          >
            {linked ? 'Update Jira' : ticket.jira_publish_unknown ? 'Publish to reconcile' : 'Promote to Jira'}
          </Button>
        )}

        {showSyncChoice && (
          <Dropdown
            align="left"
            menuLabel="Sync with Jira"
            minWidthClass="w-max"
            trigger={(open) => (
              <Button
                ref={syncTriggerRef}
                size="sm"
                variant="secondary"
                aria-haspopup="menu"
                aria-expanded={open}
              >
                Sync with Jira…
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="text-text-tertiary">
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              </Button>
            )}
            items={[
              { key: 'to_jira', label: `Overwrite ${linked!.issue_key} in Jira…` },
              { key: 'from_jira', label: `Overwrite this ticket from ${linked!.issue_key}…` },
            ]}
            onSelect={(key) => {
              // The menu item unmounts with the menu: park focus on the
              // trigger so the dialog hands it back there on close.
              syncTriggerRef.current?.focus();
              if (key === 'to_jira') setPromoteMode('overwrite');
              else setPullMode('replace');
            }}
          />
        )}

        {showOverwriteOnly && (
          <Button size="sm" variant="secondary" onClick={() => setPromoteMode('overwrite')}>
            Overwrite {linked!.issue_key} in Jira…
          </Button>
        )}

        {showUpdateFromJira && (
          <Button size="sm" variant="ghost" onClick={() => setPullMode('update')}>
            Update from Jira
          </Button>
        )}

        {showLink && (
          <Button
            ref={linkToggleRef}
            size="sm"
            variant="ghost"
            aria-expanded={linkFormOpen}
            aria-controls={linkFormOpen ? linkFormId : undefined}
            onClick={() => (linkFormOpen ? closeLinkForm() : setLinkOpen(true))}
          >
            Link existing issue
          </Button>
        )}

        {showUnlink && (
          <Button size="sm" variant="ghost" onClick={() => setConfirmUnlink(true)}>
            Unlink
          </Button>
        )}
      </div>

      {linked && link && (link.sprint || link.parent) && (
        <JiraHierarchy
          link={link}
          ticket={ticket}
          onNavigateTicket={onNavigateTicket}
        />
      )}

      {linkFormOpen && (
        <LinkExistingIssueForm
          id={linkFormId}
          projectId={projectId}
          ticketId={ticket.id}
          onCancel={closeLinkForm}
          onLinked={handleLinked}
        />
      )}

      {showUpgradeHint && (
        <p className="text-xs text-text-tertiary mt-1">
          Promote to Jira is available on the Enterprise plan.
        </p>
      )}

      {ticket.jira_publish_unknown && (
        <p className="text-xs text-text-tertiary mt-1">
          Publish result unknown — publish again to reconcile.
        </p>
      )}

      {attached && (
        <p className="text-xs text-text-tertiary mt-1">
          {showSyncChoice
            ? 'Linked from Jira — not published from SessionFS yet. Choose which side to keep when they differ.'
            : 'Linked from Jira — not published from SessionFS yet.'}
        </p>
      )}

      {promoteMode && (
        <PromoteToJiraDialog
          projectId={projectId}
          ticketId={ticket.id}
          orgId={orgId}
          overwrite={promoteMode === 'overwrite'}
          onClose={() => setPromoteMode(null)}
        />
      )}

      {pullMode && linked && (
        <ConfirmPullDialog
          projectId={projectId}
          ticketId={ticket.id}
          issueKey={linked.issue_key}
          replacing={pullMode === 'replace'}
          onClose={() => setPullMode(null)}
        />
      )}

      {confirmUnlink && (
        <ConfirmUnlinkDialog
          projectId={projectId}
          ticketId={ticket.id}
          issueKey={linked!.issue_key}
          onClose={() => setConfirmUnlink(false)}
        />
      )}
    </div>
  );
}

const ticketLinkClass =
  'font-mono text-brand hover:underline outline-none rounded-sm focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]';

/**
 * Where the linked issue sits in Jira: its sprint and its parent (or epic).
 * Read from the cached link, so it shows only what Jira last reported; with
 * neither, nothing renders.
 */
function JiraHierarchy({
  link,
  ticket,
  onNavigateTicket,
}: {
  link: JiraLink;
  ticket: Ticket;
  onNavigateTicket?: (ticketId: string) => void;
}) {
  return (
    <div role="group" aria-label="Jira planning" className="mt-2">
      <dl className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs">
        {link.sprint && <SprintItem sprint={link.sprint} />}
        {link.parent && (
          <ParentItem
            parent={link.parent}
            issueUrl={link.issue_url}
            ticket={ticket}
            onNavigateTicket={onNavigateTicket}
          />
        )}
      </dl>
    </div>
  );
}

function SprintItem({ sprint }: { sprint: JiraLinkSprint }) {
  const state = jiraSprintStateLabel(sprint.state);
  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <dt className="text-text-tertiary">Sprint</dt>
      <dd className="flex min-w-0 items-center gap-1.5">
        <span className="truncate text-text-primary">{jiraSprintName(sprint)}</span>
        {state && <Badge size="sm" variant={jiraSprintStateVariant(sprint.state)} label={state} />}
      </dd>
    </div>
  );
}

function ParentItem({
  parent,
  issueUrl,
  ticket,
  onNavigateTicket,
}: {
  parent: JiraLinkParent;
  issueUrl: string;
  ticket: Ticket;
  onNavigateTicket?: (ticketId: string) => void;
}) {
  const href = jiraSiblingIssueUrl(issueUrl, parent.key);
  const ticketId = parent.ticket_id;
  // The ticket's own parent lives in this project: open it in place. Any
  // other readable ticket may sit in another project, so look it up first.
  const sameProject = !!ticketId && ticketId === ticket.parent_ticket_id && !!onNavigateTicket;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
      <dt className="text-text-tertiary">Parent</dt>
      <dd className="flex min-w-0 flex-wrap items-center gap-x-1.5">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className={`inline-flex items-center gap-1 ${ticketLinkClass}`}
          >
            {parent.key}
            <span className="sr-only"> in Jira (opens in a new tab)</span>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              <polyline points="15 3 21 3 21 9" />
              <line x1="10" y1="14" x2="21" y2="3" />
            </svg>
          </a>
        ) : (
          <span className="font-mono text-text-primary">{parent.key}</span>
        )}
        {ticketId && sameProject && (
          <>
            <span aria-hidden="true" className="text-text-tertiary">·</span>
            <button
              type="button"
              className={ticketLinkClass}
              onClick={() => onNavigateTicket!(ticketId)}
            >
              {ticketId}
              <span className="sr-only"> (open the parent ticket)</span>
            </button>
          </>
        )}
        {ticketId && !sameProject && (
          <OtherProjectTicketLink
            ticketId={ticketId}
            currentProjectId={ticket.project_id}
            onNavigateTicket={onNavigateTicket}
          />
        )}
      </dd>
    </div>
  );
}

/** Link to a readable ticket that may live in another project. Renders
 *  nothing until the lookup says where it is (or if it can't be read). */
function OtherProjectTicketLink({
  ticketId,
  currentProjectId,
  onNavigateTicket,
}: {
  ticketId: string;
  currentProjectId: string;
  onNavigateTicket?: (ticketId: string) => void;
}) {
  const preview = useTicketPreview(ticketId);
  const projectId = preview.data?.project_id;
  if (!projectId) return null;
  if (projectId === currentProjectId && onNavigateTicket) {
    // Turns out to be in this project after all: open it in place.
    return (
      <>
        <span aria-hidden="true" className="text-text-tertiary">·</span>
        <button type="button" className={ticketLinkClass} title={preview.data?.title} onClick={() => onNavigateTicket(ticketId)}>
          {ticketId}
          <span className="sr-only"> (open the parent ticket)</span>
        </button>
      </>
    );
  }
  const to = `/projects/${encodeURIComponent(projectId)}?tab=work&sub=tickets&ticket=${encodeURIComponent(ticketId)}`;
  return (
    <>
      <span aria-hidden="true" className="text-text-tertiary">·</span>
      <Link to={to} className={ticketLinkClass} title={preview.data?.title}>
        {ticketId}
        <span className="sr-only"> (open the parent ticket)</span>
      </Link>
    </>
  );
}

/**
 * Inline "Link existing issue" form: one key field that also takes a pasted
 * Jira link, checked client-side before anything is sent. Escape cancels.
 */
function LinkExistingIssueForm({
  id,
  projectId,
  ticketId,
  onCancel,
  onLinked,
}: {
  id: string;
  projectId: string;
  ticketId: string;
  onCancel: () => void;
  onLinked: () => void;
}) {
  const link = useLinkJiraIssue(projectId);
  const { addToast } = useToast();
  const [value, setValue] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const inputId = `${id}-key`;

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (link.isPending) return;
    setFormError(null);
    const parsed = parseJiraIssueInput(value);
    const key = parsed?.key;
    if (!key) {
      setFieldError(value.trim() ? INVALID_KEY_MESSAGE : 'Enter a Jira issue key.');
      document.getElementById(inputId)?.focus();
      return;
    }
    // Show the normalised key (a pasted link becomes "ABC-123").
    setValue(key);
    setFieldError(null);
    try {
      const result = await link.mutateAsync({ ticketId, issueKey: key });
      // Say which site the key was looked up on: a link pasted from another
      // Jira site is resolved against this organization's connection.
      const site = urlHost(result.issue_url);
      if (parsed.host && site && parsed.host !== site) {
        addToast(
          'warning',
          `Linked ${result.issue_key} on ${site}, this organization’s Jira site — not ${parsed.host}, where the pasted link points.`,
        );
      } else {
        addToast('success', site ? `Linked ${result.issue_key} on ${site}` : `Linked ${result.issue_key}`);
      }
      onLinked();
    } catch (err: unknown) {
      if (jiraErrorCode(err) === 'jira_already_linked') {
        // Linked meanwhile (another tab or person): the ticket refetch swaps
        // this form for the linked badge, so report it and keep focus in the
        // section rather than flash an error that is about to unmount.
        addToast('info', 'This ticket is already linked to a Jira issue.');
        onLinked();
        return;
      }
      const { field, message } = linkErrorMessage(err);
      if (field) setFieldError(message);
      else setFormError(message);
      // Keep the key editable and focused so the fix is one keystroke away.
      document.getElementById(inputId)?.focus();
    }
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key !== 'Escape') return;
    // Claim the key either way so a surrounding drawer doesn't close; while
    // a link is in flight the form stays open until it settles.
    e.preventDefault();
    e.stopPropagation();
    if (!link.isPending) onCancel();
  }

  return (
    <form
      id={id}
      aria-label="Link an existing Jira issue"
      noValidate
      onSubmit={handleSubmit}
      onKeyDown={handleKeyDown}
      className="mt-2 max-w-sm space-y-2.5 rounded-lg border border-border bg-surface p-3"
    >
      <Input
        id={inputId}
        title="Jira issue key"
        placeholder="PROJ-123"
        autoFocus
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        value={value}
        // Read-only (not disabled) while linking so focus stays in the field.
        readOnly={link.isPending}
        onChange={(e) => {
          setValue(e.target.value);
          if (fieldError) setFieldError(null);
        }}
        hint={fieldError ? undefined : 'Paste a key or the issue’s Jira link.'}
        error={fieldError ?? undefined}
        className="font-mono"
      />
      {formError && (
        <Alert variant="danger">{formError}</Alert>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" variant="primary" loading={link.isPending}>
          Link issue
        </Button>
        <Button type="button" size="sm" variant="ghost" disabled={link.isPending} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/** The Jira issue link. `issue_url` comes from the server's stored link; only
 *  render it as a clickable anchor when it parses to an http(s) URL. */
function LinkedIssueBadge({
  issueKey,
  issueUrl,
  status,
}: {
  issueKey: string;
  issueUrl: string;
  status: string;
}) {
  const href = safeHttpUrl(issueUrl);
  const chip = `inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs ${
    href ? 'hover:border-border-strong' : ''
  }`;
  const badge = (
    <>
      <span className="text-text-tertiary">Jira</span>
      <span className="font-mono font-medium text-text-primary">{issueKey}</span>
      <span aria-hidden="true" className="text-text-tertiary">·</span>
      <span className="text-text-secondary">{status}</span>
      {href && (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="text-text-tertiary">
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
          <polyline points="15 3 21 3 21 9" />
          <line x1="10" y1="14" x2="21" y2="3" />
        </svg>
      )}
    </>
  );
  if (!href) {
    return <span className={chip}>{badge}</span>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`${chip} outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`}
    >
      {badge}
    </a>
  );
}

function ConfirmUnlinkDialog({
  projectId,
  ticketId,
  issueKey,
  onClose,
}: {
  projectId: string;
  ticketId: string;
  issueKey: string;
  onClose: () => void;
}) {
  const unlink = useUnlinkJira(projectId);
  const { addToast } = useToast();
  const [error, setError] = useState<string | null>(null);

  async function handleUnlink() {
    setError(null);
    try {
      await unlink.mutateAsync(ticketId);
      addToast('success', `Unlinked ${issueKey}`);
      onClose();
    } catch (err: unknown) {
      setError(friendlyError(err));
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="jira-unlink-heading">
      <DialogHeader titleId="jira-unlink-heading">Unlink {issueKey}?</DialogHeader>
      <p className="text-sm text-text-secondary">
        Remove the link between this ticket and its Jira issue. The Jira issue itself is left
        untouched, and you can promote the ticket again to create a fresh one.
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
        <Button variant="danger" disabled={unlink.isPending} onClick={handleUnlink}>
          {unlink.isPending ? 'Unlinking…' : 'Unlink'}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

/** Messages for a failed pull (Jira → ticket). */
function pullErrorMessage(err: unknown): string {
  const code = jiraErrorCode(err);
  if (code === 'jira_not_linked') return 'This ticket is no longer linked to a Jira issue.';
  if (code === 'jira_issue_not_found') {
    return 'The Jira issue no longer exists, or the Jira connection can’t see it.';
  }
  if (err instanceof ApiError && err.status === 403 && (!code || /^\d+$/.test(code) || code === 'project_edit_forbidden')) {
    return 'You don’t have permission to update this ticket.';
  }
  const message = friendlyError(err);
  return message === 'Could not load the Jira draft' ? 'Could not update the ticket from Jira.' : message;
}

function joinFields(fields: string[]): string {
  return fields.length > 1 ? `${fields.slice(0, -1).join(', ')} and ${fields[fields.length - 1]}` : fields[0];
}

/**
 * Confirm taking the Jira issue's text into the ticket. Says exactly what is
 * overwritten (the ticket's title and description) and what is kept.
 */
function ConfirmPullDialog({
  projectId,
  ticketId,
  issueKey,
  replacing,
  onClose,
}: {
  projectId: string;
  ticketId: string;
  issueKey: string;
  /** Chosen from the sync menu: phrase it as overwriting this side. */
  replacing: boolean;
  onClose: () => void;
}) {
  const pull = usePullJira(projectId);
  const { addToast } = useToast();
  const [error, setError] = useState<string | null>(null);

  async function handlePull() {
    setError(null);
    try {
      const result = await pull.mutateAsync(ticketId);
      const fields = result.updated_fields ?? [];
      if (fields.length === 0) addToast('info', `No changes from Jira ${issueKey}`);
      else addToast('success', `Updated the ${joinFields(fields)} from ${issueKey}`);
      onClose();
    } catch (err: unknown) {
      setError(pullErrorMessage(err));
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="jira-pull-heading">
      <DialogHeader titleId="jira-pull-heading">
        {replacing ? `Overwrite this ticket from ${issueKey}?` : `Update this ticket from ${issueKey}?`}
      </DialogHeader>
      <div className="space-y-2 text-sm text-text-secondary">
        <p>
          This ticket’s <span className="font-medium text-text-primary">title and description</span> are
          overwritten with the summary and description of{' '}
          <span className="font-mono font-medium text-text-primary">{issueKey}</span> in Jira; the replaced
          text here is not kept. A field that’s empty in Jira is left as it is.
        </p>
        <p>Acceptance criteria, comments, status and everything else on the ticket stay as they are.</p>
      </div>
      {error && (
        <Alert variant="danger" className="mt-3">
          {error}
        </Alert>
      )}
      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button variant="danger" loading={pull.isPending} onClick={handlePull}>
          Replace title and description
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

/* ── Preview + publish dialog ── */

interface PromoteToJiraDialogProps {
  projectId: string;
  ticketId: string;
  /** Opened to replace an issue that was linked from Jira (explicit choice). */
  overwrite?: boolean;
  /** The project's org, so settings links open on the right organization. */
  orgId?: string | null;
  onClose: () => void;
}

function templateChoiceLabel(t: { name: string; version: number | null }): string {
  return t.version != null ? `${t.name} v${t.version}` : t.name;
}

export function PromoteToJiraDialog({ projectId, ticketId, overwrite = false, orgId, onClose }: PromoteToJiraDialogProps) {
  const choices = useTicketJiraTemplates(projectId, ticketId, true);
  const [picked, setPicked] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The sprint lives here, not in the draft form: the form remounts whenever
  // the draft is rebuilt (template change) and the choice must survive that.
  const [sprint, setSprint] = useState('');
  // A publish Jira accepted with warnings: the dialog stays open on them.
  const [published, setPublished] = useState<JiraLink | null>(null);
  // Wait for the template list before the first preview so it renders with the
  // picker's starting choice. An older server without the list endpoint (or
  // an error, surfaced by the preview below) falls back to the inherited draft.
  const choicesSettled = choices.isSuccess || choices.isError;
  const selected = choices.data ? (picked ?? defaultJiraTemplateChoice(choices.data)) : undefined;
  // Once published, the draft is gone (publish clears it); don't rebuild it.
  const preview = useJiraPreview(projectId, ticketId, choicesSettled && !published, selected);
  const sprints = useJiraSprints(projectId, !published);
  const queryClient = useQueryClient();

  const options = useMemo(() => {
    const list = choices.data?.templates ?? [];
    // This ticket's pinned template is listed even after it was archived (a
    // published ticket keeps its template); label it so it's not mistaken for
    // a normal choice.
    const opts = list.map((t) => ({
      value: t.slug,
      label: t.archived ? `${templateChoiceLabel(t)} (archived)` : templateChoiceLabel(t),
    }));
    // The inherited default may be an archived template (still in use by the
    // mapping/connection): keep it selectable, labelled.
    const inherited = choices.data?.inherited_default;
    if (inherited && !list.some((t) => t.slug === inherited.slug)) {
      opts.push({ value: inherited.slug, label: `${templateChoiceLabel(inherited)} (archived)` });
    }
    return opts;
  }, [choices.data]);

  function handleClose() {
    // Drop the cached draft so the next open rebuilds it from the live ticket
    // rather than seeding the form with a now-stale preview.
    queryClient.removeQueries({ queryKey: ['jiraPreview', projectId, ticketId] });
    onClose();
  }

  function handleTemplateChanged() {
    setNotice(TEMPLATE_CHANGED_MESSAGE);
    void preview.refetch?.();
    void choices.refetch?.();
  }

  if (published) {
    return (
      <Dialog open onClose={handleClose} titleId="jira-promote-heading" className="max-w-xl">
        <PublishedWithWarnings result={published} onDone={handleClose} />
      </Dialog>
    );
  }

  return (
    <Dialog open onClose={handleClose} titleId="jira-promote-heading" className="max-w-3xl">
      <DialogHeader titleId="jira-promote-heading">
        {overwrite || preview.data?.existing?.attached
          ? `Replace ${preview.data?.existing?.issue_key ?? 'the Jira issue'} with this ticket`
          : preview.data?.existing
            ? 'Update Jira issue'
            : 'Promote to Jira'}
      </DialogHeader>

      {choices.data && options.length > 0 && (
        <div className="mb-3">
          <Select
            title="Template"
            value={selected ?? ''}
            onValueChange={(v) => {
              setNotice(null);
              setPicked(v);
            }}
            options={options}
            hint="Defaults to the template this ticket was published with, else the project's template."
          />
        </div>
      )}

      {(preview.data || preview.isLoading) && (
        <SprintPicker
          query={sprints}
          orgId={orgId}
          value={sprint}
          onChange={setSprint}
          republish={!!preview.data?.existing}
        />
      )}

      {notice && (
        <Alert variant="warning" className="mb-3">
          {notice}
        </Alert>
      )}

      {(preview.isLoading || (!choicesSettled && !preview.data)) && (
        <p className="text-sm text-text-tertiary">Building draft…</p>
      )}

      {preview.isError && (
        <Alert variant="danger">{friendlyError(preview.error)}</Alert>
      )}

      {preview.data && (
        <JiraDraftForm
          key={preview.dataUpdatedAt}
          projectId={projectId}
          ticketId={ticketId}
          preview={preview.data}
          isFetching={preview.isFetching}
          overwrite={overwrite}
          sprintId={sprintIdFromChoice(sprint)}
          orgId={orgId}
          onClose={handleClose}
          onPublishedWithWarnings={setPublished}
          onTemplateChanged={handleTemplateChanged}
        />
      )}
    </Dialog>
  );
}

function JiraDraftForm({
  projectId,
  ticketId,
  preview,
  isFetching,
  overwrite = false,
  sprintId,
  orgId,
  onClose,
  onPublishedWithWarnings,
  onTemplateChanged,
}: {
  projectId: string;
  ticketId: string;
  preview: JiraPreview;
  isFetching?: boolean;
  overwrite?: boolean;
  /** The picked sprint, or undefined to leave the sprint alone. */
  sprintId?: number;
  orgId?: string | null;
  onClose: () => void;
  /** Jira took the publish but left something out: show why instead of closing. */
  onPublishedWithWarnings?: (result: JiraLink) => void;
  onTemplateChanged?: () => void;
}) {
  const publish = usePublishJira(projectId);
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  // The dialog is mounted once per open; `preview` is available on first
  // render, so the editable draft seeds directly from it (no effect).
  const [summary, setSummary] = useState(preview.summary);
  const [sections, setSections] = useState<JiraSection[]>(preview.sections);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [sprintFieldMissing, setSprintFieldMissing] = useState(false);
  // Publishing over an issue that was linked from Jira replaces its text, so
  // the draft says so and the publish carries overwrite_existing. A stale
  // view learns it from the server's 409 and asks here instead of sending.
  const [overwriteNeeded, setOverwriteNeeded] = useState(overwrite || !!preview.existing?.attached);
  const scrollRef = useRef<HTMLDivElement>(null);
  const note = templateNote(preview);
  const existingKey = preview.existing?.issue_key ?? 'the Jira issue';

  function updateParagraph(sectionKey: string, index: number, text: string) {
    setSections((prev) =>
      prev.map((s) =>
        s.key === sectionKey
          ? {
              ...s,
              paragraphs: s.paragraphs.map((p, i) => (i === index ? { ...p, text } : p)),
            }
          : s,
      ),
    );
  }

  async function handlePublish() {
    setPublishError(null);
    setSprintFieldMissing(false);
    try {
      const result = await publish.mutateAsync({
        ticketId,
        body: {
          summary,
          // The server resolves `mode` from the ticket's template by section
          // key and rejects unknown keys, so send only the editable shape.
          sections: sections.map((s) => ({
            key: s.key,
            heading: s.heading,
            paragraphs: s.paragraphs.map((p) => ({ text: p.text, citations: p.citations })),
          })),
          confirm: true,
          // Echo the template this draft was built with: the server refuses
          // (409 template_changed) rather than publish a different version.
          template: preview.template.slug,
          template_version: preview.template.version,
          ...(overwriteNeeded ? { overwrite_existing: true } : {}),
          ...(sprintId !== undefined ? { sprint_id: sprintId } : {}),
        },
      });
      const warnings = result?.warnings ?? [];
      if (warnings.length > 0 && onPublishedWithWarnings) {
        onPublishedWithWarnings(result);
        return;
      }
      addToast('success', overwriteNeeded ? `Replaced ${existingKey} in Jira` : 'Published to Jira');
      onClose();
    } catch (err: unknown) {
      if (jiraErrorCode(err) === 'template_changed' && onTemplateChanged) {
        onTemplateChanged();
        return;
      }
      if (jiraErrorCode(err) === 'jira_overwrite_required') {
        // Nothing was sent to Jira. Show the overwrite warning and let the
        // user confirm it with the (now explicit) replace button.
        setOverwriteNeeded(true);
        scrollRef.current?.scrollTo?.({ top: 0 });
        return;
      }
      if (jiraErrorCode(err) === 'jira_sprint_field_unknown') {
        // Nothing was sent to Jira. Only an org admin can fix the connection;
        // anyone can still publish without a sprint.
        setSprintFieldMissing(true);
        // The sprints list now knows too: refetch it so the picker gives way
        // to its "no sprint field" note (and the choice is cleared).
        void queryClient.invalidateQueries({ queryKey: ['jiraSprints', projectId] });
        return;
      }
      setPublishError(friendlyError(err));
    }
  }

  return (
    <>
      <div ref={scrollRef} className="space-y-4 max-h-[70vh] overflow-y-auto pr-1">
        {overwriteNeeded ? (
          <Alert variant="warning" title={`This replaces ${existingKey} in Jira`}>
            {existingKey} was linked from Jira and hasn’t been published from SessionFS. Publishing
            overwrites its <strong>summary and description</strong> in Jira with the draft below; the
            current Jira text is not kept.
          </Alert>
        ) : (
          preview.existing && (
            <Alert variant="info" title={`Republishing ${preview.existing.issue_key}`}>
              This ticket is already linked; publishing updates the existing issue.
              {preview.diff && preview.diff.length > 0
                ? ' Changes are shown below as a diff against what was last published.'
                : ''}
            </Alert>
          )
        )}

        {note && <p className="text-xs text-text-tertiary">{note}</p>}

        <div>
          <p className="text-sm font-medium text-text-secondary mb-1">
            {preview.llm_used
              ? `Rewrite sections drafted by your model (${preview.key_source}).`
              : 'Built from the template only (no model rewrite).'}
          </p>
        </div>

        <div>
          <label htmlFor="jira-summary" className="text-sm font-medium text-text-secondary block mb-1">
            Summary
          </label>
          <Input
            id="jira-summary"
            value={summary}
            maxLength={MAX_SUMMARY}
            onChange={(e) => setSummary(e.target.value)}
            error={summary.length === 0 ? 'Summary is required' : undefined}
          />
          <p className="text-xs text-text-tertiary mt-0.5 tabular-nums text-right">
            {summary.length}/{MAX_SUMMARY}
          </p>
        </div>

        {preview.diff && preview.diff.length > 0 && (
          <div>
            <h3 className="text-xs font-semibold text-text-secondary mb-1">Changes</h3>
            <pre className="whitespace-pre-wrap text-xs font-mono bg-bg-sunken border border-border rounded-lg p-3 text-text-primary">
              {preview.diff.join('\n')}
            </pre>
          </div>
        )}

        {sections.map((section) => (
          <section key={section.key} aria-label={section.heading || section.key}>
            <div className="flex items-baseline gap-2 mb-1">
              <h3 className="text-sm font-semibold text-text-primary">{section.heading || section.key}</h3>
              <Badge
                variant={section.mode === 'verbatim' ? 'default' : 'info'}
                label={section.mode === 'verbatim' ? 'verbatim' : 'rewritten'}
              />
            </div>
            {section.paragraphs.map((p, i) => (
              <div key={i} className="mb-2">
                {section.mode === 'verbatim' ? (
                  <p className="whitespace-pre-wrap text-sm text-text-primary bg-bg-sunken border border-border rounded-lg p-2">
                    {p.text}
                  </p>
                ) : (
                  <Textarea
                    value={p.text}
                    onChange={(e) => updateParagraph(section.key, i, e.target.value)}
                    aria-label={`${section.heading || section.key} paragraph ${i + 1}`}
                  />
                )}
                {p.citations.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {p.citations.map((c) => (
                      <span
                        key={c}
                        className="text-2xs font-mono text-text-tertiary bg-surface border border-border rounded px-1 py-0.5"
                      >
                        {c}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </section>
        ))}

        {publishError && <Alert variant="danger">{publishError}</Alert>}
        {sprintFieldMissing && (
          <Alert variant="danger" title="Jira sprint field unknown">
            This organization’s Jira connection doesn’t know the site’s sprint field, so the issue
            can’t be put in a sprint. Clear the sprint to publish now, and ask an org admin to run{' '}
            <strong>Detect fields</strong> in{' '}
            <Link to={jiraConnectionSettingsHref(orgId)} className="text-brand underline">
              Organization settings → Integrations → Jira → Connection
            </Link>
            .
          </Alert>
        )}
      </div>

      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant={overwriteNeeded ? 'danger' : 'primary'}
          disabled={publish.isPending || summary.trim() === '' || isFetching}
          loading={publish.isPending}
          onClick={handlePublish}
        >
          {overwriteNeeded ? `Replace ${existingKey}` : preview.existing ? 'Publish update' : 'Publish'}
        </Button>
      </DialogFooter>
    </>
  );
}

/* ── Sprint picker + publish warnings ── */

/** Only numeric sprint ids can be sent (`sprint_id` is an int on the server). */
function sprintIdFromChoice(value: string): number | undefined {
  if (!/^\d+$/.test(value)) return undefined;
  const id = Number(value);
  return Number.isSafeInteger(id) ? id : undefined;
}

function sprintDescription(s: JiraSprint): string | undefined {
  const start = jiraSprintDateLabel(s.start_date);
  const end = jiraSprintDateLabel(s.end_date);
  const dates = start && end ? `${start} – ${end}` : start ? `from ${start}` : end ? `until ${end}` : null;
  const parts = [s.board_name, dates].filter(Boolean);
  return parts.length ? parts.join(' · ') : undefined;
}

/** Why the publish can't pick a sprint, for the `unavailable` codes the sprints list returns. */
function sprintUnavailableMessage(code: string): string {
  switch (code) {
    case 'jira_software_not_available':
      return 'Sprints aren’t available: this Jira site doesn’t have Jira Software boards for the mapped project, so the issue is published without a sprint.';
    case 'jira_sprint_field_unknown':
      return 'Sprints aren’t available: this organization’s Jira connection doesn’t know the site’s sprint field, so no sprint can be set. Ask an org admin to run Detect fields in the Jira connection settings.';
    default:
      return 'Sprints aren’t available for this project, so the issue is published without a sprint.';
  }
}

const SPRINT_GROUP: Record<string, string> = { active: 'Active sprints', future: 'Future sprints' };

/**
 * Optional sprint for the publish. Fed by the project's sprints endpoint;
 * a refusal or outage leaves just a quiet note, since a sprint is never
 * required. Without Jira Software the site has no sprints at all, and the
 * picker says so instead.
 */
function SprintPicker({
  query,
  orgId,
  value,
  onChange,
  republish,
}: {
  query: { data?: JiraSprintList; isLoading: boolean; isError: boolean; error: unknown };
  orgId?: string | null;
  value: string;
  onChange: (value: string) => void;
  /** Updating an existing issue: no choice leaves its sprint as it is. */
  republish: boolean;
}) {
  const options = useMemo<ComboboxOption[]>(() => {
    const none: ComboboxOption = {
      value: '',
      label: republish ? 'Don’t change the sprint' : 'No sprint',
      description: republish ? 'Leave the issue in the sprint it’s in now' : 'Publish to the backlog',
    };
    const list = sortJiraSprints(query.data?.sprints ?? []).filter((s) => sprintIdFromChoice(String(s.id)) !== undefined);
    return [
      none,
      ...list.map((s) => {
        const state = (s.state ?? '').toLowerCase();
        return {
          value: String(s.id),
          label: jiraSprintName(s),
          description: sprintDescription(s),
          group: Object.hasOwn(SPRINT_GROUP, state) ? SPRINT_GROUP[state] : 'Other sprints',
          searchText: `${s.name} ${s.board_name ?? ''}`,
        };
      }),
    ];
  }, [query.data, republish]);

  // A sprint that left the list (closed, or the list failed) can't be sent.
  useEffect(() => {
    if (value && !options.some((o) => o.value === value)) onChange('');
  }, [options, value, onChange]);

  // Not an editor (or an older server): no picker, no noise.
  if (query.isError && query.error instanceof ApiError && [403, 404, 405].includes(query.error.status)) {
    return null;
  }

  if (query.data?.unavailable) {
    return (
      <div className="mb-3">
        <p className="text-sm font-medium text-text-secondary mb-1">Sprint</p>
        <p className="text-xs text-text-tertiary">
          {sprintUnavailableMessage(query.data.unavailable)}
          {query.data.unavailable === 'jira_sprint_field_unknown' && (
            <>
              {' '}
              <Link to={jiraConnectionSettingsHref(orgId)} className="text-brand underline">
                Open the Jira connection settings
              </Link>
            </>
          )}
        </p>
      </div>
    );
  }

  const sprintCount = options.length - 1;
  return (
    <div className="mb-3">
      <Combobox
        title="Sprint"
        value={value}
        onValueChange={(v) => onChange(v)}
        options={options}
        placeholder="No sprint"
        searchPlaceholder="Search sprints or boards"
        loading={query.isLoading}
        noMatchesMessage={(q) => `No sprints match “${q}”`}
        hint={
          query.isError
            ? 'Sprints couldn’t be loaded from Jira; you can still publish without one.'
            : query.isLoading
              ? 'Loading sprints…'
              : sprintCount === 0
                ? 'No active or future sprints on this project’s Jira boards.'
                : 'Optional. Active sprints are listed first.'
        }
      />
    </div>
  );
}

/** Jira took the publish but left parts out; say what, one sentence each. */
function PublishedWithWarnings({ result, onDone }: { result: JiraLink; onDone: () => void }) {
  const warnings = result.warnings ?? [];
  const href = safeHttpUrl(result.issue_url);
  return (
    <>
      <DialogHeader titleId="jira-promote-heading">Published {result.issue_key}, with notes</DialogHeader>
      <div className="space-y-3">
        <p className="text-sm text-text-secondary">
          The issue is in Jira
          {href ? (
            <>
              {' '}
              (
              <a href={href} target="_blank" rel="noopener noreferrer" className="font-mono text-brand underline">
                {result.issue_key}
                <span className="sr-only"> (opens in a new tab)</span>
              </a>
              )
            </>
          ) : null}
          , but Jira didn’t take everything:
        </p>
        <Alert variant="warning">
          <ul className="list-disc space-y-1 pl-4">
            {warnings.map((code) => (
              <li key={code}>{jiraPublishWarningMessage(code)}</li>
            ))}
          </ul>
        </Alert>
      </div>
      <DialogFooter>
        <Button variant="primary" autoFocus onClick={onDone}>
          Done
        </Button>
      </DialogFooter>
    </>
  );
}
