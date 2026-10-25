import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { JiraPreview, JiraSection, Ticket } from '../../api/client';
import { Alert, Badge, Button, Dialog, DialogFooter, DialogHeader, Input, Textarea } from '../../components/ui';
import { useJiraLink, useJiraPreview, usePublishJira, useUnlinkJira } from '../../hooks/useJiraTickets';
import { useToast } from '../../hooks/useToast';
import { errorMessage } from '../../utils/errorMessage';
import { jiraErrorCode } from '../../utils/jira';
import { safeHttpUrl } from '../../utils/url';

/* ── Ticket-detail Jira surface (Enterprise, org-scoped projects) ──
 *
 * The button appears for any org-scoped project (the connection GET is
 * admin-only, so a member learns connection/mapping state from the preview
 * POST's error codes inside the dialog). The badge reflects the ticket's
 * embedded link summary, with the live status refreshed via GET /jira. */

const MAX_SUMMARY = 255;

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
    default:
      return errorMessage(err, 'Could not load the Jira draft');
  }
}

interface JiraTicketSectionProps {
  projectId: string;
  ticket: Ticket;
  orgId: string;
  isProjectAdmin: boolean;
  isEnterprise: boolean;
}

export function JiraTicketSection({ projectId, ticket, orgId, isProjectAdmin, isEnterprise }: JiraTicketSectionProps) {
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [confirmUnlink, setConfirmUnlink] = useState(false);

  const linked = ticket.jira ?? null;
  // Refresh the live status only when there is a confirmed link; in-progress
  // and unknown tickets have no issue key to GET yet.
  const linkQuery = useJiraLink(projectId, ticket.id, !!linked);
  const link = linkQuery.data ?? null;

  const showPromote = !!orgId && isEnterprise;
  const showUpgradeHint = !!orgId && !isEnterprise;
  const showUnlink = isProjectAdmin && !!linked;

  if (!showPromote && !showUpgradeHint && !linked && !ticket.jira_publish_in_progress && !ticket.jira_publish_unknown) {
    return null;
  }

  return (
    <div className="pt-1">
      <h4 className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mb-1">
        Jira
      </h4>

      <div className="flex flex-wrap items-center gap-2">
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

        {showPromote && (
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setPromoteOpen(true)}
          >
            {linked ? 'Update Jira' : ticket.jira_publish_unknown ? 'Publish to reconcile' : 'Promote to Jira'}
          </Button>
        )}

        {showUnlink && (
          <Button size="sm" variant="ghost" onClick={() => setConfirmUnlink(true)}>
            Unlink
          </Button>
        )}
      </div>

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

      {promoteOpen && (
        <PromoteToJiraDialog
          projectId={projectId}
          ticketId={ticket.id}
          onClose={() => setPromoteOpen(false)}
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
  const badge = (
    <>
      <Badge variant="info" label={issueKey} />
      <span className={`text-xs text-text-secondary ${href ? 'hover:text-brand' : ''}`}>
        {status}
      </span>
    </>
  );
  if (!href) {
    return <span className="inline-flex items-center gap-1.5">{badge}</span>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded"
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

/* ── Preview + publish dialog ── */

interface PromoteToJiraDialogProps {
  projectId: string;
  ticketId: string;
  onClose: () => void;
}

export function PromoteToJiraDialog({ projectId, ticketId, onClose }: PromoteToJiraDialogProps) {
  const preview = useJiraPreview(projectId, ticketId, true);
  const queryClient = useQueryClient();

  function handleClose() {
    // Drop the cached draft so the next open rebuilds it from the live ticket
    // rather than seeding the form with a now-stale preview.
    queryClient.removeQueries({ queryKey: ['jiraPreview', projectId, ticketId] });
    onClose();
  }

  return (
    <Dialog open onClose={handleClose} titleId="jira-promote-heading" className="max-w-3xl">
      <DialogHeader titleId="jira-promote-heading">
        {preview.data?.existing ? 'Update Jira issue' : 'Promote to Jira'}
      </DialogHeader>

      {preview.isLoading && <p className="text-sm text-text-tertiary">Building draft…</p>}

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
          onClose={handleClose}
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
  onClose,
}: {
  projectId: string;
  ticketId: string;
  preview: JiraPreview;
  isFetching?: boolean;
  onClose: () => void;
}) {
  const publish = usePublishJira(projectId);
  const { addToast } = useToast();
  // The dialog is mounted once per open; `preview` is available on first
  // render, so the editable draft seeds directly from it (no effect).
  const [summary, setSummary] = useState(preview.summary);
  const [sections, setSections] = useState<JiraSection[]>(preview.sections);
  const [publishError, setPublishError] = useState<string | null>(null);

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
    try {
      await publish.mutateAsync({
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
        },
      });
      addToast('success', 'Published to Jira');
      onClose();
    } catch (err: unknown) {
      setPublishError(friendlyError(err));
    }
  }

  return (
    <>
      <div className="space-y-4 max-h-[70vh] overflow-y-auto pr-1">
        {preview.existing && (
          <Alert variant="info" title={`Republishing ${preview.existing.issue_key}`}>
            This ticket is already linked; publishing updates the existing issue. Changes are shown
            below as a diff against what was last published.
          </Alert>
        )}

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
      </div>

      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="primary"
          disabled={publish.isPending || summary.trim() === '' || isFetching}
          loading={publish.isPending}
          onClick={handlePublish}
        >
          {preview.existing ? 'Publish update' : 'Publish'}
        </Button>
      </DialogFooter>
    </>
  );
}
