import { useSearchParams } from 'react-router-dom';
import { TabPanel, Tabs } from '../../components/ui';
import { useContactSuggestions } from '../../hooks/useContacts';
import { ContactDetail } from './ContactDetail';
import { ContactList } from './ContactList';
import { SuggestionsQueue } from './SuggestionsQueue';

type View = 'directory' | 'confirm';

/**
 * Organization → Contacts: people the org attributes work to who never sign
 * in (GitHub #125). Every member can browse, create and edit; owners and
 * admins also merge, delete and export. "To confirm" lists names from
 * meetings and chats nobody has matched yet — a person turns them into a
 * contact, a model never does.
 *
 * URL: `?tab=contacts&view=confirm` for the queue, `&contact=<id>` for a
 * contact's page.
 */
export default function ContactsSection({ orgId, isAdmin }: { orgId: string; isAdmin: boolean }) {
  const [params, setParams] = useSearchParams();
  const contactId = params.get('contact');
  const view: View = params.get('view') === 'confirm' ? 'confirm' : 'directory';
  const suggestions = useContactSuggestions(orgId);
  const pendingCount = suggestions.data?.groups.length ?? 0;

  const setParam = (key: string, value: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value === null) next.delete(key);
        else next.set(key, value);
        if (key === 'view') next.delete('contact');
        return next;
      },
      { replace: key === 'view' },
    );

  if (contactId) {
    return (
      <ContactDetail
        orgId={orgId}
        contactId={contactId}
        isAdmin={isAdmin}
        onBack={() => setParam('contact', null)}
        onOpen={(id) => setParam('contact', id)}
      />
    );
  }

  return (
    <div className="space-y-4">
      <Tabs
        bare
        variant="underline"
        idPrefix="contacts"
        aria-label="Contact views"
        activeKey={view}
        onChange={(k) => setParam('view', k === 'confirm' ? 'confirm' : null)}
        tabs={[
          { key: 'directory', label: 'Contacts' },
          {
            key: 'confirm',
            label: (
              <span className="inline-flex items-center gap-1.5">
                To confirm
                {pendingCount > 0 && (
                  <span className="rounded-full bg-surface-hover px-1.5 text-2xs font-semibold tabular-nums text-text-secondary">
                    <span className="sr-only">(</span>
                    {suggestions.data?.truncated ? `${pendingCount}+` : pendingCount}
                    <span className="sr-only"> names)</span>
                  </span>
                )}
              </span>
            ),
          },
        ]}
      />
      <TabPanel idPrefix="contacts" tabKey={view}>
        {view === 'confirm' ? (
          <SuggestionsQueue orgId={orgId} query={suggestions} onOpenContact={(id) => setParam('contact', id)} />
        ) : (
          <ContactList orgId={orgId} isAdmin={isAdmin} onOpen={(id) => setParam('contact', id)} />
        )}
      </TabPanel>
    </div>
  );
}
