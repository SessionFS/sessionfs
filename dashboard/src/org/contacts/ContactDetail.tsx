import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, type ContactReferences, type ExternalContact } from '../../api/client';
import { Alert, Badge, Button, Card, Input, Tooltip } from '../../components/ui';
import { useAuth } from '../../auth/AuthContext';
import { useToast } from '../../hooks/useToast';
import {
  useContact,
  useContactAlias,
  useContactReferences,
  useDeleteContact,
  useUpdateContact,
} from '../../hooks/useContacts';
import ConfirmDialog from '../groups/ConfirmDialog';
import { ContactAvatar, ExternalBadge } from './ContactChip';
import { ContactFormDialog } from './ContactFormDialog';
import { MergeContactDialog } from './MergeContactDialog';
import { contactErrorMessage, conversationHref, shortDate, ticketHref } from './format';

/**
 * One contact: who they are, every name transcripts use for them, and where
 * they appear (only meetings, chats and tickets you can read). Members edit
 * and archive; owners and admins also merge, export and delete.
 */
export function ContactDetail({
  orgId,
  contactId,
  isAdmin,
  onBack,
  onOpen,
}: {
  orgId: string;
  contactId: string;
  isAdmin: boolean;
  onBack: () => void;
  /** Open another contact (a merged id's target, a merge result). */
  onOpen: (id: string) => void;
}) {
  const contact = useContact(orgId, contactId);
  const c = contact.data;
  // References load for the contact's own id (a merged id redirects first).
  const refs = useContactReferences(orgId, c?.id === contactId ? contactId : null);

  // A merged id answers with its target: show the target's own URL.
  useEffect(() => {
    if (c && c.id !== contactId) onOpen(c.id);
  }, [c, contactId, onOpen]);

  const back = (
    <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2">
      <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polyline points="15 18 9 12 15 6" />
      </svg>
      All contacts
    </Button>
  );

  if (contact.isLoading) {
    return (
      <div>
        {back}
        <p className="py-10 text-center text-sm text-text-tertiary">Loading contact…</p>
      </div>
    );
  }
  if (contact.isError || !c) {
    const gone = contact.error instanceof ApiError && contact.error.status === 404;
    return (
      <div className="space-y-3">
        {back}
        <Alert variant={gone ? 'info' : 'danger'}>
          {gone ? 'This contact doesn’t exist or was deleted.' : `Couldn’t load the contact: ${contactErrorMessage(contact.error)}`}
        </Alert>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {back}
      <ContactHeader orgId={orgId} contact={c} isAdmin={isAdmin} refs={refs.data} onBack={onBack} onOpen={onOpen} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <AliasesCard orgId={orgId} contact={c} />
        <ReferencesCard orgId={orgId} refs={refs.data} loading={refs.isLoading} error={refs.error} />
      </div>
    </div>
  );
}

function ContactHeader({
  orgId,
  contact: c,
  isAdmin,
  refs,
  onBack,
  onOpen,
}: {
  orgId: string;
  contact: ExternalContact;
  isAdmin: boolean;
  refs: ContactReferences | undefined;
  onBack: () => void;
  onOpen: (id: string) => void;
}) {
  const { auth } = useAuth();
  const { addToast } = useToast();
  const update = useUpdateContact(orgId);
  const del = useDeleteContact(orgId);
  const [editing, setEditing] = useState(false);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [merging, setMerging] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const archived = c.status === 'archived';

  async function setStatus(next: 'active' | 'archived') {
    setActionError(null);
    try {
      await update.mutateAsync({ contactId: c.id, body: { status: next } });
      addToast('success', next === 'archived' ? `Archived ${c.display_name}` : `Restored ${c.display_name}`);
      setConfirmArchive(false);
    } catch (e) {
      setActionError(contactErrorMessage(e));
    }
  }

  async function remove() {
    setActionError(null);
    try {
      const r = await del.mutateAsync(c.id);
      addToast('success', `Deleted ${c.display_name}${r.tickets_unassigned ? ` · ${r.tickets_unassigned} ticket${r.tickets_unassigned === 1 ? '' : 's'} unassigned` : ''}`);
      setConfirmDelete(false);
      onBack();
    } catch (e) {
      setActionError(contactErrorMessage(e));
    }
  }

  async function exportJson() {
    if (!auth) return;
    setExporting(true);
    try {
      const data = await auth.client.exportContact(orgId, c.id);
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = `contact-${c.id}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      addToast('error', `Export failed: ${contactErrorMessage(e)}`);
    } finally {
      setExporting(false);
    }
  }

  const ticketCount = refs?.tickets.length;
  const mentionCount = refs?.conversations.length;

  return (
    <Card level="elevated" className="rounded-xl">
      <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <ContactAvatar name={c.display_name} className="!h-10 !w-10 !text-base" />
          <div className="min-w-0">
            <h2 className="flex flex-wrap items-center gap-2 text-lg font-semibold text-text-primary">
              <span className="min-w-0 break-words">{c.display_name}</span>
              <ExternalBadge linked={!!c.linked_user_id} archived={archived} />
            </h2>
            {c.role && <p className="text-sm text-text-secondary">{c.role}</p>}
            <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
              <dt className="text-text-tertiary">Email</dt>
              <dd className="min-w-0 text-text-secondary [overflow-wrap:anywhere]">
                {c.email ? (
                  <>
                    <span className="font-mono text-xs">{c.email}</span>
                    {c.email_masked && <span className="ml-1 text-xs text-text-tertiary">(partly hidden)</span>}
                  </>
                ) : c.has_email ? (
                  'Hidden'
                ) : (
                  'None'
                )}
              </dd>
              {c.tags.length > 0 && (
                <>
                  <dt className="text-text-tertiary">Tags</dt>
                  <dd className="flex flex-wrap gap-1">
                    {c.tags.map((t) => (
                      <Badge key={t} label={t} size="sm" />
                    ))}
                  </dd>
                </>
              )}
              <dt className="text-text-tertiary">Added</dt>
              <dd className="text-text-secondary">{shortDate(c.created_at)}</dd>
            </dl>
            {c.linked_user_id && (
              <p className="mt-2 text-sm text-text-tertiary">
                Now a member of this organization (matched by verified email{c.linked_at ? ` on ${shortDate(c.linked_at)}` : ''}).
                Their meetings resolve to the member.
              </p>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2 sm:justify-end">
          <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button variant="secondary" size="sm" onClick={() => (archived ? void setStatus('active') : setConfirmArchive(true))} disabled={update.isPending}>
            {archived ? 'Restore' : 'Archive'}
          </Button>
          {isAdmin && (
            <>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setMerging(true)}
                disabled={archived}
                disabledReason="Restore this contact before merging it"
              >
                Merge…
              </Button>
              <Button variant="secondary" size="sm" onClick={exportJson} disabled={exporting}>
                {exporting ? 'Exporting…' : 'Export data'}
              </Button>
              <Button variant="danger" size="sm" onClick={() => setConfirmDelete(true)}>
                Delete
              </Button>
            </>
          )}
        </div>
      </div>
      {actionError && !confirmArchive && !confirmDelete && (
        <div className="px-5 pb-5">
          <Alert variant="danger">{actionError}</Alert>
        </div>
      )}

      {editing && <ContactFormDialog orgId={orgId} contact={c} onClose={() => setEditing(false)} />}
      {merging && (
        <MergeContactDialog
          orgId={orgId}
          target={c}
          onClose={() => setMerging(false)}
          onMerged={(t) => {
            setMerging(false);
            onOpen(t.id);
          }}
        />
      )}
      <ConfirmDialog
        open={confirmArchive}
        title={`Archive ${c.display_name}?`}
        confirmLabel="Archive"
        pending={update.isPending}
        error={confirmArchive ? actionError : null}
        onConfirm={() => void setStatus('archived')}
        onClose={() => {
          setConfirmArchive(false);
          setActionError(null);
        }}
      >
        <p className="text-sm text-text-secondary">
          They stay on the meetings and tickets that already name them, but new meetings stop matching them and they
          leave the assignee pickers. You can restore them any time.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirmDelete}
        title={`Delete ${c.display_name}?`}
        confirmLabel="Delete contact"
        danger
        pending={del.isPending}
        error={confirmDelete ? actionError : null}
        onConfirm={() => void remove()}
        onClose={() => {
          setConfirmDelete(false);
          setActionError(null);
        }}
      >
        <div className="space-y-2 text-sm text-text-secondary">
          <p>This erases the contact and its other names for good. It can’t be undone.</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>
              {mentionCount === undefined
                ? 'Meeting and chat participants matched to them go back to unmatched.'
                : `${mentionCount} meeting or chat mention${mentionCount === 1 ? '' : 's'} you can see go back to unmatched.`}
            </li>
            <li>
              {ticketCount === undefined
                ? 'Tickets assigned to them become unassigned.'
                : `${ticketCount} ticket${ticketCount === 1 ? '' : 's'} you can see become${ticketCount === 1 ? 's' : ''} unassigned.`}{' '}
              The ticket history records only that a contact was removed, not their name.
            </li>
            <li>Transcripts keep the name as written; meeting retention covers those.</li>
          </ul>
        </div>
      </ConfirmDialog>
    </Card>
  );
}

function AliasesCard({ orgId, contact: c }: { orgId: string; contact: ExternalContact }) {
  const alias = useContactAlias(orgId);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const full = c.aliases.length >= 20;

  async function add(e: FormEvent) {
    e.preventDefault();
    const value = draft.trim();
    if (!value) return;
    setError(null);
    try {
      await alias.mutateAsync({ contactId: c.id, alias: value });
      setDraft('');
    } catch (err) {
      setError(contactErrorMessage(err));
    }
  }

  async function remove(value: string) {
    setError(null);
    try {
      await alias.mutateAsync({ contactId: c.id, alias: value, remove: true });
    } catch (err) {
      setError(contactErrorMessage(err));
    }
  }

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="contact-aliases-heading" className="space-y-3 p-5">
        <div>
          <h3 id="contact-aliases-heading" className="text-sm font-semibold text-text-primary">
            Other names
          </h3>
          <p className="mt-1 text-xs text-text-tertiary">
            A transcript that names “{c.display_name}” or any of these matches this contact, as long as no other contact
            uses the same name. Matching is exact, never fuzzy.
          </p>
        </div>
        {c.aliases.length === 0 ? (
          <p className="text-sm text-text-tertiary">No other names yet.</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5" aria-label="Other names">
            {c.aliases.map((a) => (
              <li key={a} className="inline-flex items-center gap-1 rounded-md border border-border bg-surface py-0.5 pl-2 pr-0.5 text-sm text-text-secondary">
                {a}
                <Tooltip content={'Remove'} describe={false}>
                  <button
                    type="button"
                    onClick={() => void remove(a)}
                    disabled={alias.isPending}
                    aria-label={`Remove other name ${a}`}
                    className="inline-flex h-5 w-5 items-center justify-center rounded text-text-tertiary hover:bg-surface-hover hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50"
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
                      <path d="M18 6 6 18M6 6l12 12" />
                    </svg>
                  </button>
                </Tooltip>
              </li>
            ))}
          </ul>
        )}
        <form onSubmit={add} className="flex items-end gap-2">
          <div className="min-w-0 flex-1">
            <Input
              id="contact-alias-new"
              title="Add another name"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              maxLength={120}
              placeholder="e.g. Tolu"
              disabled={full || alias.isPending}
              hint={full ? 'A contact can have at most 20 other names.' : undefined}
            />
          </div>
          <Button type="submit" variant="secondary" disabled={full || !draft.trim()} loading={alias.isPending}>
            Add
          </Button>
        </form>
        {error && <Alert variant="danger">{error}</Alert>}
      </section>
    </Card>
  );
}

function ReferencesCard({
  orgId,
  refs,
  loading,
  error,
}: {
  orgId: string;
  refs: ContactReferences | undefined;
  loading: boolean;
  error: unknown;
}) {
  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="contact-refs-heading" className="space-y-4 p-5">
        <div>
          <h3 id="contact-refs-heading" className="text-sm font-semibold text-text-primary">
            Where they appear
          </h3>
          <p className="mt-1 text-xs text-text-tertiary">Only meetings, chats and tickets you can open are listed.</p>
        </div>
        {loading ? (
          <div className="h-20 rounded-md bg-bg-tertiary animate-pulse" aria-busy="true" aria-label="Loading references" />
        ) : error ? (
          <Alert variant="danger">Couldn’t load where they appear: {contactErrorMessage(error)}</Alert>
        ) : !refs || (refs.conversations.length === 0 && refs.tickets.length === 0 && refs.action_items.length === 0) ? (
          <p className="text-sm text-text-tertiary">Nothing you can see names this contact yet.</p>
        ) : (
          <>
            <RefGroup title="Tickets" count={refs.tickets.length}>
              {refs.tickets.map((t) => (
                <li key={t.id} className="flex items-center justify-between gap-3">
                  <Link to={ticketHref(t)} className="min-w-0 truncate text-sm text-text-primary hover:underline rounded-sm outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
                    {t.title}
                  </Link>
                  <Badge label={t.status.replace(/_/g, ' ')} size="sm" />
                </li>
              ))}
            </RefGroup>
            <RefGroup title="Action items" count={refs.action_items.length}>
              {refs.action_items.map((i) => {
                const meeting = refs.conversations.find((cv) => cv.meeting_id === i.meeting_id);
                const href = meeting ? conversationHref(meeting, orgId) : null;
                const text = (
                  <span className="block min-w-0 truncate">
                    {i.kind === 'decision' ? 'Decision: ' : ''}
                    {i.description}
                  </span>
                );
                return (
                  <li key={i.item_id} className="flex items-center justify-between gap-3 text-sm text-text-primary">
                    {href ? (
                      <Link to={href} className="min-w-0 truncate hover:underline rounded-sm outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
                        {text}
                      </Link>
                    ) : (
                      text
                    )}
                    <span className="shrink-0 text-xs text-text-tertiary">{i.due_date ? `Due ${shortDate(i.due_date)}` : i.status}</span>
                  </li>
                );
              })}
            </RefGroup>
            <RefGroup title="Meetings and chats" count={refs.conversations.length}>
              {refs.conversations.map((cv) => {
                const href = conversationHref(cv, orgId);
                return (
                  <li key={cv.participant_id} className="flex items-center justify-between gap-3">
                    {href ? (
                      <Link to={href} className="min-w-0 truncate text-sm text-text-primary hover:underline rounded-sm outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
                        {cv.title}
                      </Link>
                    ) : (
                      <span className="min-w-0 truncate text-sm text-text-primary">{cv.title}</span>
                    )}
                    <span className="shrink-0 text-xs text-text-tertiary">
                      {cv.kind === 'chat' ? 'Chat' : 'Meeting'} · {shortDate(cv.meeting_at ?? cv.created_at)}
                    </span>
                  </li>
                );
              })}
            </RefGroup>
          </>
        )}
      </section>
    </Card>
  );
}

function RefGroup({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  if (count === 0) return null;
  return (
    <div>
      <h4 className="mb-1.5 text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary">
        {title} <span className="tabular-nums">({count})</span>
      </h4>
      <ul className="space-y-1.5">{children}</ul>
    </div>
  );
}
