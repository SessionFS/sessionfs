import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ContactSuggestionGroup, ContactSuggestions } from '../../api/client';
import { Alert, Button, Card, Checkbox, Combobox, type ComboboxOption } from '../../components/ui';
import { useToast } from '../../hooks/useToast';
import {
  useActiveContacts,
  useApplyContactSuggestion,
  useContactAlias,
  useDismissContactSuggestion,
} from '../../hooks/useContacts';
import { ContactAvatar } from './ContactChip';
import { ContactFormDialog } from './ContactFormDialog';
import { contactErrorMessage, shortDate } from './format';
import { Term } from '../../help/Term';

/**
 * "To confirm": names in the last 90 days of meetings and chats you can read
 * that match no one. Nothing here is stored: the list is computed from the
 * unmatched participants, so a model can never write a contact. Each name
 * becomes a new contact, another name of an existing one, or is dismissed
 * (for you only).
 */
export function SuggestionsQueue({
  orgId,
  query,
  onOpenContact,
}: {
  orgId: string;
  query: UseQueryResult<ContactSuggestions, unknown>;
  onOpenContact: (id: string) => void;
}) {
  const contacts = useActiveContacts(orgId);

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="to-confirm-heading" className="space-y-4 p-5">
        <div>
          <h2 id="to-confirm-heading" className="text-base font-semibold text-text-primary">
            <Term id="people-to-confirm">People to confirm</Term>
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-text-tertiary">
            Names from the last 90 days of meetings and chats you can see that don’t match anyone. Confirm who each one
            is and every mention you can edit is matched. Mentions in conversations you can’t edit match the next time
            they’re extracted.
          </p>
        </div>
        {query.isLoading ? (
          <div className="space-y-2" aria-busy="true" aria-label="Loading names to confirm">
            {[0, 1].map((i) => (
              <div key={i} className="h-20 rounded-md bg-bg-tertiary animate-pulse" />
            ))}
          </div>
        ) : query.isError ? (
          <Alert variant="danger">Couldn’t load names to confirm: {contactErrorMessage(query.error)}</Alert>
        ) : !query.data || query.data.groups.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center">
            <p className="text-sm text-text-secondary">Nothing to confirm.</p>
            <p className="mt-1 text-sm text-text-tertiary">Every name in recent meetings and chats is matched or dismissed.</p>
          </div>
        ) : (
          <>
            <ul className="space-y-3">
              {query.data.groups.map((g) => (
                <SuggestionRow
                  key={g.name_norm}
                  orgId={orgId}
                  group={g}
                  contactOptions={contacts.data?.items ?? []}
                  contactsLoading={contacts.isLoading}
                  onOpenContact={onOpenContact}
                />
              ))}
            </ul>
            {query.data.truncated && (
              <p className="text-xs text-text-tertiary">Showing the first {query.data.groups.length} names. More appear as you confirm these.</p>
            )}
          </>
        )}
      </section>
    </Card>
  );
}

type Mode = null | 'existing' | 'create';

function SuggestionRow({
  orgId,
  group: g,
  contactOptions,
  contactsLoading,
  onOpenContact,
}: {
  orgId: string;
  group: ContactSuggestionGroup;
  contactOptions: { id: string; display_name: string; role: string | null; aliases: string[] }[];
  contactsLoading: boolean;
  onOpenContact: (id: string) => void;
}) {
  const { addToast } = useToast();
  const apply = useApplyContactSuggestion(orgId);
  const alias = useContactAlias(orgId);
  const dismiss = useDismissContactSuggestion(orgId);
  const [mode, setMode] = useState<Mode>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = apply.isPending || alias.isPending || dismiss.isPending;
  const headingId = `suggestion-${g.name_norm.replace(/[^a-z0-9_-]/gi, '_')}`;

  const options = useMemo<ComboboxOption[]>(() => {
    const hinted = new Map(g.candidates.map((c) => [c.id, c.hint]));
    return [...contactOptions]
      .sort((a, b) => Number(hinted.has(b.id)) - Number(hinted.has(a.id)) || a.display_name.localeCompare(b.display_name))
      .map((c) => ({
        value: c.id,
        label: c.display_name,
        description: c.role || undefined,
        meta: hinted.get(c.id),
        icon: <ContactAvatar name={c.display_name} />,
        group: hinted.has(c.id) ? 'Likely' : 'All contacts',
        searchText: `${c.display_name} ${c.aliases.join(' ')}`,
      }));
  }, [contactOptions, g.candidates]);

  /** Match every editable mention to the contact, optionally remembering the name. */
  async function resolveTo(contactId: string, contactName: string, rememberName: boolean) {
    setError(null);
    try {
      if (rememberName) await alias.mutateAsync({ contactId, alias: g.name });
      const r = await apply.mutateAsync({ name: g.name, contactId });
      const skipped = r.skipped ? ` · ${r.skipped} in conversations you can’t edit` : '';
      addToast('success', `Matched ${r.applied} mention${r.applied === 1 ? '' : 's'} of “${g.name}” to ${contactName}${skipped}`);
      setMode(null);
    } catch (e) {
      setError(contactErrorMessage(e));
    }
  }

  async function onDismiss() {
    setError(null);
    try {
      await dismiss.mutateAsync(g.name);
      addToast('success', `Dismissed “${g.name}”`);
    } catch (e) {
      setError(contactErrorMessage(e));
    }
  }

  return (
    <li className="rounded-lg border border-border bg-bg-primary p-3" aria-labelledby={headingId}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p id={headingId} className="font-medium text-text-primary">
            “{g.name}”
          </p>
          <p className="text-xs text-text-tertiary">
            {g.mention_count} mention{g.mention_count === 1 ? '' : 's'}
            {g.latest_mention_at ? ` · latest ${shortDate(g.latest_mention_at)}` : ''}
          </p>
          {g.sources.length > 0 && (
            <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs" aria-label={`Where “${g.name}” is mentioned`}>
              {g.sources.map((s) => (
                <li key={s.meeting_id} className="min-w-0 max-w-full">
                  <Link
                    to={`/meetings/${encodeURIComponent(s.meeting_id)}`}
                    className="block truncate text-brand hover:underline rounded-sm outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  >
                    {s.kind === 'chat' ? 'Chat: ' : ''}
                    {s.title}
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {g.candidates.length > 0 && mode === null && (
            <p className="mt-1.5 text-xs text-text-secondary">
              Might be{' '}
              {g.candidates.map((c, i) => (
                <span key={c.id}>
                  {i > 0 && ', '}
                  <button
                    type="button"
                    onClick={() => onOpenContact(c.id)}
                    className="text-text-primary underline-offset-2 hover:underline rounded-sm outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  >
                    {c.display_name}
                  </button>{' '}
                  <span className="text-text-tertiary">({c.hint})</span>
                </span>
              ))}
            </p>
          )}
        </div>
        {mode === null && (
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button size="sm" onClick={() => setMode('create')} disabled={busy}>
              Create contact
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setMode('existing')} disabled={busy}>
              Existing contact…
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void onDismiss()} disabled={busy} aria-label={`Dismiss “${g.name}”`}>
              Dismiss
            </Button>
          </div>
        )}
      </div>

      {mode === 'existing' && (
        <ExistingPicker
          name={g.name}
          options={options}
          loading={contactsLoading}
          busy={busy}
          onPick={(id, remember) => {
            const c = contactOptions.find((x) => x.id === id);
            void resolveTo(id, c?.display_name ?? 'the contact', remember);
          }}
          onCancel={() => setMode(null)}
        />
      )}
      {mode === 'create' && (
        <ContactFormDialog
          orgId={orgId}
          initialName={g.name}
          onClose={() => setMode((m) => (m === 'create' ? null : m))}
          onSaved={(c) => void resolveTo(c.id, c.display_name, false)}
          onUseExisting={(id) => {
            setMode('existing');
            const c = contactOptions.find((x) => x.id === id);
            void resolveTo(id, c?.display_name ?? 'the contact', true);
          }}
        />
      )}
      {error && (
        <Alert variant="danger" className="mt-3">
          {error}
        </Alert>
      )}
    </li>
  );
}

function ExistingPicker({
  name,
  options,
  loading,
  busy,
  onPick,
  onCancel,
}: {
  name: string;
  options: ComboboxOption[];
  loading: boolean;
  busy: boolean;
  onPick: (contactId: string, rememberName: boolean) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState('');
  const [remember, setRemember] = useState(true);
  const rememberId = `remember-${name.replace(/[^a-z0-9_-]/gi, '_')}`;
  return (
    <div className="mt-3 space-y-2 border-t border-border pt-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-0 flex-1 basis-56">
          <Combobox
            title={`Who is “${name}”?`}
            value={value}
            onValueChange={setValue}
            options={options}
            placeholder="Choose a contact"
            searchPlaceholder="Search contacts"
            loading={loading}
            disabled={busy}
            emptyMessage="No contacts yet. Create one instead."
          />
        </div>
        <Button size="sm" onClick={() => value && onPick(value, remember)} disabled={!value} loading={busy}>
          Match
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
      <Checkbox
        id={rememberId}
        checked={remember}
        onChange={(e) => setRemember(e.target.checked)}
        disabled={busy}
        label={`Remember “${name}” as another name, so future meetings match it`}
      />
    </div>
  );
}
