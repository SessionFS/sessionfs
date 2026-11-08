import { useEffect, useState } from 'react';
import type { ExternalContact } from '../../api/client';
import { Alert, Badge, Button, Card, Input, Select, TableShell } from '../../components/ui';
import { useAuth } from '../../auth/AuthContext';
import { useContactList } from '../../hooks/useContacts';
import { ContactAvatar, ExternalBadge } from './ContactChip';
import { ContactFormDialog } from './ContactFormDialog';
import { contactErrorMessage, shortDate } from './format';

type StatusFilter = 'active' | 'archived' | 'all';

const STATUS_OPTIONS = [
  { value: 'active', label: 'Active' },
  { value: 'archived', label: 'Archived' },
  { value: 'all', label: 'All' },
];

function useDebounced(value: string, ms = 250): string {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** The org's contact directory: search, status and tag filters, newest first. */
export function ContactList({ orgId, isAdmin, onOpen }: { orgId: string; isAdmin: boolean; onOpen: (id: string) => void }) {
  const { auth } = useAuth();
  const [q, setQ] = useState('');
  const [status, setStatus] = useState<StatusFilter>('active');
  const [tag, setTag] = useState('');
  const debouncedQ = useDebounced(q.trim());
  const debouncedTag = useDebounced(tag.trim().toLowerCase());
  const list = useContactList(orgId, {
    q: debouncedQ || undefined,
    status,
    tag: debouncedTag || undefined,
  });
  const rows = list.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = !!(debouncedQ || debouncedTag || status !== 'active');

  const [creating, setCreating] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [download, setDownload] = useState<{ url: string; truncated: boolean } | null>(null);
  useEffect(() => () => {
    if (download) URL.revokeObjectURL(download.url);
  }, [download]);

  async function exportCsv() {
    if (!auth) return;
    setExporting(true);
    setExportError(null);
    try {
      const { blob, truncated } = await auth.client.exportContactsCsv(orgId, status);
      setDownload({ url: URL.createObjectURL(blob), truncated });
    } catch (e) {
      setExportError(contactErrorMessage(e));
    } finally {
      setExporting(false);
    }
  }

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="contacts-heading" className="space-y-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="contacts-heading" className="text-base font-semibold text-text-primary">
              External contacts
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-text-tertiary">
              People your organization works with who don’t use SessionFS. Meetings, chats and tickets can name them so
              their work is attributed to one person, not six spellings. They’re never emailed or notified.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {isAdmin && (
              <Button variant="secondary" onClick={exportCsv} disabled={exporting}>
                {exporting ? 'Preparing…' : 'Export CSV'}
              </Button>
            )}
            <Button onClick={() => setCreating(true)}>New contact</Button>
          </div>
        </div>

        {exportError && <Alert variant="danger">Could not export contacts: {exportError}</Alert>}
        {download && (
          <Alert variant="success" title="Export ready">
            <a href={download.url} download={`contacts-${orgId}.csv`} className="font-medium text-brand underline">
              Download contacts.csv
            </a>
            {download.truncated && ' The export stops at 10,000 rows.'}
          </Alert>
        )}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Input
            id="contacts-search"
            title="Search"
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Name or other name"
            maxLength={120}
          />
          <Select title="Status" value={status} onValueChange={(v) => setStatus(v as StatusFilter)} options={STATUS_OPTIONS} />
          <Input id="contacts-tag" title="Tag" value={tag} onChange={(e) => setTag(e.target.value)} placeholder="e.g. vendor" maxLength={32} />
        </div>

        {list.isError ? (
          <Alert variant="danger">Couldn’t load contacts: {contactErrorMessage(list.error)}</Alert>
        ) : list.isLoading ? (
          <div className="space-y-2" aria-busy="true" aria-label="Loading contacts">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-11 rounded-md bg-bg-tertiary animate-pulse" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center">
            <p className="text-sm text-text-secondary">{filtered ? 'No contacts match these filters.' : 'No contacts yet.'}</p>
            {!filtered && (
              <p className="mt-1 text-sm text-text-tertiary">
                Create one, or confirm names from meetings and chats under “To confirm”.
              </p>
            )}
          </div>
        ) : (
          <>
            <TableShell label="Contacts" minWidth={640} bordered>
              <thead>
                <tr className="border-b border-border text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary">
                  <th scope="col" className="px-3 py-2">Name</th>
                  <th scope="col" className="px-3 py-2">Email</th>
                  <th scope="col" className="px-3 py-2">Tags</th>
                  <th scope="col" className="px-3 py-2">Status</th>
                  <th scope="col" className="px-3 py-2 text-right">Updated</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <ContactRow key={c.id} contact={c} onOpen={onOpen} />
                ))}
              </tbody>
            </TableShell>
            {list.hasNextPage && (
              <div className="flex justify-center">
                <Button variant="secondary" onClick={() => void list.fetchNextPage()} loading={list.isFetchingNextPage}>
                  Load more
                </Button>
              </div>
            )}
          </>
        )}
      </section>
      {creating && <ContactFormDialog orgId={orgId} onClose={() => setCreating(false)} onSaved={(c) => onOpen(c.id)} />}
    </Card>
  );
}

function ContactRow({ contact: c, onOpen }: { contact: ExternalContact; onOpen: (id: string) => void }) {
  return (
    <tr className="border-b border-border last:border-b-0 hover:bg-surface-hover">
      <td className="px-3 py-2.5">
        <button
          type="button"
          onClick={() => onOpen(c.id)}
          className="flex min-w-0 items-center gap-2 rounded-sm text-left outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          <ContactAvatar name={c.display_name} className="!h-6 !w-6 !text-xs" />
          <span className="min-w-0">
            <span className="block truncate font-medium text-text-primary hover:underline">{c.display_name}</span>
            {(c.role || c.aliases.length > 0) && (
              <span className="block truncate text-xs text-text-tertiary">
                {[c.role, c.aliases.length ? `also “${c.aliases.slice(0, 2).join('”, “')}”${c.aliases.length > 2 ? '…' : ''}` : null]
                  .filter(Boolean)
                  .join(' · ')}
              </span>
            )}
          </span>
        </button>
      </td>
      <td className="px-3 py-2.5 text-text-secondary">
        {c.email ? (
          <span className="font-mono text-xs" title={c.email_masked ? 'Hidden: only owners, admins and the creator see the full address' : undefined}>
            {c.email}
            {c.email_masked && <span className="sr-only"> (partly hidden)</span>}
          </span>
        ) : c.has_email ? (
          <span className="text-xs text-text-tertiary">Hidden</span>
        ) : (
          <span className="text-text-tertiary" aria-label="No email">—</span>
        )}
      </td>
      <td className="px-3 py-2.5">
        <span className="flex flex-wrap gap-1">
          {c.tags.map((t) => (
            <Badge key={t} label={t} size="sm" />
          ))}
        </span>
      </td>
      <td className="px-3 py-2.5">
        {c.linked_user_id ? (
          <ExternalBadge linked />
        ) : (
          <Badge label={c.status === 'archived' ? 'Archived' : 'Active'} variant={c.status === 'archived' ? 'default' : 'success'} tint size="sm" />
        )}
      </td>
      <td className="px-3 py-2.5 text-right text-xs text-text-tertiary tabular-nums">{shortDate(c.updated_at)}</td>
    </tr>
  );
}
