import { useMemo, useState } from 'react';
import type { ContactMergeResult, ExternalContact } from '../../api/client';
import { Alert, Button, Combobox, Dialog, DialogFooter, DialogHeader, type ComboboxOption } from '../../components/ui';
import { useToast } from '../../hooks/useToast';
import { useActiveContacts, useMergeContacts } from '../../hooks/useContacts';
import { ContactAvatar } from './ContactChip';
import { contactErrorMessage } from './format';

/**
 * Owners/admins: fold a duplicate into this contact. Pick the duplicate, see
 * what moves (the server's preview), then confirm. The duplicate's name
 * becomes another name of this contact; its meetings and tickets move here.
 */
export function MergeContactDialog({
  orgId,
  target,
  onClose,
  onMerged,
}: {
  orgId: string;
  target: ExternalContact;
  onClose: () => void;
  onMerged: (target: ExternalContact) => void;
}) {
  const contacts = useActiveContacts(orgId);
  const merge = useMergeContacts(orgId);
  const { addToast } = useToast();
  const [sourceId, setSourceId] = useState('');
  const [preview, setPreview] = useState<ContactMergeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const titleId = 'merge-contact-title';
  const pending = merge.isPending;

  const options = useMemo<ComboboxOption[]>(
    () =>
      (contacts.data?.items ?? [])
        .filter((c) => c.id !== target.id)
        .sort((a, b) => a.display_name.localeCompare(b.display_name))
        .map((c) => ({
          value: c.id,
          label: c.display_name,
          description: [c.role, c.email].filter(Boolean).join(' · ') || undefined,
          icon: <ContactAvatar name={c.display_name} />,
          searchText: `${c.display_name} ${c.aliases.join(' ')} ${c.email ?? ''}`,
        })),
    [contacts.data, target.id],
  );
  const source = contacts.data?.items.find((c) => c.id === sourceId);

  async function pick(id: string) {
    setSourceId(id);
    setPreview(null);
    setError(null);
    if (!id) return;
    try {
      setPreview(await merge.mutateAsync({ targetId: target.id, sourceId: id, confirm: false }));
    } catch (e) {
      setError(contactErrorMessage(e));
    }
  }

  async function confirm() {
    if (!sourceId) return;
    setError(null);
    try {
      const r = await merge.mutateAsync({ targetId: target.id, sourceId, confirm: true });
      addToast('success', `Merged ${source?.display_name ?? 'the duplicate'} into ${r.target.display_name}`);
      onMerged(r.target);
    } catch (e) {
      setError(contactErrorMessage(e));
    }
  }

  return (
    <Dialog open onClose={() => !pending && onClose()} titleId={titleId}>
      <DialogHeader titleId={titleId}>Merge into {target.display_name}</DialogHeader>
      <p className="-mt-2 mb-4 text-sm text-text-tertiary">
        Choose the duplicate. It’s removed, and everything that names it moves to {target.display_name}.
      </p>
      <Combobox
        id="merge-source"
        title="Duplicate contact"
        value={sourceId}
        onValueChange={(v) => void pick(v)}
        options={options}
        placeholder="Choose a contact"
        searchPlaceholder="Search contacts"
        loading={contacts.isLoading}
        disabled={pending}
        emptyMessage="No other active contacts"
      />
      {pending && !preview && sourceId && <p className="mt-3 text-sm text-text-tertiary" role="status">Checking what moves…</p>}
      {preview && source && (
        <div className="mt-4 rounded-lg border border-border bg-surface p-3 text-sm text-text-secondary" data-testid="merge-preview">
          <p className="font-medium text-text-primary">What happens</p>
          <ul className="mt-1.5 list-disc space-y-1 pl-5">
            <li>
              {preview.participants_moved} meeting or chat mention{preview.participants_moved === 1 ? '' : 's'} move
              {preview.participants_moved === 1 ? 's' : ''} to {target.display_name}.
            </li>
            <li>
              {preview.tickets_moved} ticket{preview.tickets_moved === 1 ? '' : 's'} assigned to {source.display_name}{' '}
              {preview.tickets_moved === 1 ? 'is' : 'are'} reassigned, with an entry in each ticket’s history.
            </li>
            <li>
              {preview.aliases_added.length > 0
                ? `New other names: ${preview.aliases_added.join(', ')}.`
                : 'No new other names.'}
            </li>
            <li>Email, role and tags fill in only where {target.display_name} has none.</li>
          </ul>
        </div>
      )}
      {error && (
        <Alert variant="danger" className="mt-4">
          {error}
        </Alert>
      )}
      <DialogFooter>
        <Button variant="secondary" onClick={onClose} disabled={pending}>
          Cancel
        </Button>
        <Button variant="danger" onClick={() => void confirm()} disabled={!preview || !!error} loading={pending && !!preview}>
          Merge
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
