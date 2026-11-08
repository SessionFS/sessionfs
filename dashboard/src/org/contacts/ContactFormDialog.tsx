import { useEffect, useMemo, useState, type FormEvent } from 'react';
import type { ExternalContact, ExternalContactUpdate } from '../../api/client';
import { Alert, Button, Dialog, DialogFooter, DialogHeader, Input } from '../../components/ui';
import { useContactLookup, useCreateContact, useUpdateContact } from '../../hooks/useContacts';
import { contactErrorCode, contactErrorMessage, existingContactId, parseTags } from './format';

const fold = (s: string) => s.normalize('NFKC').toLocaleLowerCase().split(/\s+/).filter(Boolean).join(' ');

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/**
 * Create or edit an external contact. Create warns, before saving, when a
 * contact already has this exact name or other name (the server still lets
 * two people share a name), and after saving lists the duplicates it found.
 * Contacts hold personal data about people outside SessionFS, so the form
 * keeps to name, email, role and tags: no notes.
 */
export function ContactFormDialog({
  orgId,
  contact,
  initialName = '',
  onClose,
  onSaved,
  onUseExisting,
}: {
  orgId: string;
  /** Edit this contact; omit to create one. */
  contact?: ExternalContact;
  /** Prefill the name (e.g. from an unmatched meeting participant). */
  initialName?: string;
  onClose: () => void;
  /** Called once saved (create: after any duplicate notice is dismissed). */
  onSaved?: (contact: ExternalContact) => void;
  /** Offer "Use this contact" on a duplicate instead of creating another. */
  onUseExisting?: (contactId: string) => void;
}) {
  const editing = !!contact;
  const create = useCreateContact(orgId);
  const update = useUpdateContact(orgId);
  const pending = create.isPending || update.isPending;

  const [name, setName] = useState(contact?.display_name ?? initialName);
  const [email, setEmail] = useState(contact && !contact.email_masked ? (contact.email ?? '') : '');
  const [role, setRole] = useState(contact?.role ?? '');
  const [tags, setTags] = useState((contact?.tags ?? []).join(', '));
  const [aliases, setAliases] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [existingId, setExistingId] = useState<string | null>(null);
  const [created, setCreated] = useState<ExternalContact | null>(null);

  // Duplicate hint while typing (create only).
  const debounced = useDebounced(name);
  const lookup = useContactLookup(editing ? null : orgId, debounced);
  const sameName = useMemo(() => {
    const key = fold(debounced);
    if (!key) return [];
    return (lookup.data?.items ?? []).filter(
      (c) => c.status !== 'merged' && (fold(c.display_name) === key || c.aliases.some((a) => fold(a) === key)),
    );
  }, [lookup.data, debounced]);

  const close = () => {
    if (!pending) onClose();
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setExistingId(null);
    const display_name = name.trim();
    if (!display_name) {
      setError('Enter a name.');
      return;
    }
    try {
      if (contact) {
        const body: ExternalContactUpdate = {};
        if (display_name !== contact.display_name) body.display_name = display_name;
        const nextRole = role.trim() || null;
        if (nextRole !== (contact.role ?? null)) body.role = nextRole;
        const nextTags = parseTags(tags);
        if (nextTags.join(',') !== contact.tags.join(',')) body.tags = nextTags;
        // A masked email can't be edited in place; only send a typed change.
        if (!contact.email_masked) {
          const nextEmail = email.trim() || null;
          if (nextEmail !== (contact.email ?? null)) body.email = nextEmail;
        } else if (email.trim()) {
          body.email = email.trim();
        }
        const saved = Object.keys(body).length ? await update.mutateAsync({ contactId: contact.id, body }) : contact;
        onSaved?.(saved);
        onClose();
        return;
      }
      const saved = await create.mutateAsync({
        display_name,
        email: email.trim() || null,
        role: role.trim() || null,
        tags: parseTags(tags),
        aliases: aliases
          .split(/[,\n]/)
          .map((a) => a.trim())
          .filter(Boolean),
      });
      if (saved.possible_duplicates.length > 0) {
        setCreated(saved);
        return;
      }
      onSaved?.(saved);
      onClose();
    } catch (err) {
      setError(contactErrorMessage(err));
      if (contactErrorCode(err) === 'contact_exists') setExistingId(existingContactId(err));
    }
  }

  const titleId = 'contact-form-title';

  if (created) {
    return (
      <Dialog open onClose={() => { onSaved?.(created); onClose(); }} titleId={titleId}>
        <DialogHeader titleId={titleId}>Contact created</DialogHeader>
        <Alert variant="warning" title="Possible duplicate">
          {created.possible_duplicates.length === 1 ? 'Another contact has' : 'Other contacts have'} the same name:{' '}
          {created.possible_duplicates.map((d) => d.display_name).join(', ')}. If it’s the same person, an owner or admin
          can merge them from the contact’s page.
        </Alert>
        <DialogFooter>
          <Button onClick={() => { onSaved?.(created); onClose(); }}>Done</Button>
        </DialogFooter>
      </Dialog>
    );
  }

  return (
    <Dialog open onClose={close} titleId={titleId}>
      <DialogHeader titleId={titleId}>{editing ? 'Edit contact' : 'New contact'}</DialogHeader>
      {!editing && (
        <p className="-mt-2 mb-4 text-sm text-text-tertiary">
          Someone your organization works with who doesn’t use SessionFS. Meetings, chats and tickets can name them;
          they’re never emailed or notified.
        </p>
      )}
      <form onSubmit={submit} className="space-y-3" noValidate>
        <Input
          id="contact-name"
          title="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={120}
          required
          autoFocus
          disabled={pending}
        />
        {!editing && sameName.length > 0 && (
          <Alert variant="warning" title="Possible duplicate">
            <span>
              {sameName.map((c) => c.display_name).join(', ')} {sameName.length === 1 ? 'already exists' : 'already exist'}.
            </span>
            {onUseExisting && (
              <span className="mt-2 flex flex-wrap gap-2">
                {sameName.slice(0, 3).map((c) => (
                  <Button key={c.id} type="button" size="sm" variant="secondary" onClick={() => onUseExisting(c.id)} disabled={pending}>
                    Use {c.display_name}
                  </Button>
                ))}
              </span>
            )}
          </Alert>
        )}
        <Input
          id="contact-email"
          title="Email (optional)"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          maxLength={320}
          placeholder={contact?.email_masked && contact.email ? `${contact.email} (hidden — type to replace)` : undefined}
          hint={
            contact?.email_masked
              ? 'Only owners, admins and whoever created this contact see the full address.'
              : 'Used to match the person in chat exports. Owners, admins and you see it in full.'
          }
          disabled={pending}
        />
        <Input
          id="contact-role"
          title="Role (optional)"
          value={role}
          onChange={(e) => setRole(e.target.value)}
          maxLength={80}
          placeholder="e.g. Clinical lead"
          disabled={pending}
        />
        <Input
          id="contact-tags"
          title="Tags (optional)"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="vendor, data-team"
          hint="Separate with commas. Lowercase letters, digits, “.”, “_”, “:” or “-”."
          disabled={pending}
        />
        {!editing && (
          <Input
            id="contact-aliases"
            title="Other names (optional)"
            value={aliases}
            onChange={(e) => setAliases(e.target.value)}
            placeholder="Tolu, T. Adeyemi"
            hint="Names transcripts use for them, separated by commas. Each one matches only this contact."
            disabled={pending}
          />
        )}
        {error && (
          <Alert variant="danger">
            {error}
            {existingId && onUseExisting && (
              <span className="mt-2 block">
                <Button type="button" size="sm" variant="secondary" onClick={() => onUseExisting(existingId)}>
                  Use that contact
                </Button>
              </span>
            )}
          </Alert>
        )}
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={close} disabled={pending}>
            Cancel
          </Button>
          <Button type="submit" loading={pending}>
            {editing ? 'Save' : 'Create contact'}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
