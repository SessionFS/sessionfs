import { Link } from 'react-router-dom';
import { Alert, Badge, Card, PageContainer, PageHeader } from '../../components/ui';
import type { NeedsYouItem, NeedsYouKind } from '../../api/reviewLoop';
import { useNeedsYouItems } from '../../hooks/useReviewLoop';
import { errorMessage } from '../../utils/errorMessage';
import { projectLoopHref, storyHref } from './format';

const KIND_LABEL: Record<NeedsYouKind, string> = {
  ready: 'Ready to merge',
  needs_human: 'Needs a person',
  proposed_checklist: 'Checklist proposal',
};

function itemHref(i: NeedsYouItem): string {
  return i.ticket_id ? storyHref(i.project_id, i.ticket_id) : projectLoopHref(i.project_id);
}

/** Group by project, keeping the server's newest-first order. */
export function groupByProject(items: NeedsYouItem[]): Array<{ id: string; name: string; items: NeedsYouItem[] }> {
  const groups = new Map<string, { id: string; name: string; items: NeedsYouItem[] }>();
  for (const i of items) {
    const g = groups.get(i.project_id) ?? { id: i.project_id, name: i.project_name, items: [] };
    g.items.push(i);
    groups.set(i.project_id, g);
  }
  return [...groups.values()];
}

/** Everything across your projects that is waiting on a person. */
export default function ReviewLoopsPage() {
  const { data, isLoading, error } = useNeedsYouItems();
  const groups = groupByProject(data?.items ?? []);
  const total = data?.counts.total ?? 0;
  const shown = data?.items.length ?? 0;

  return (
    <PageContainer width="narrow">
      <PageHeader
        title="Review loops"
        description="Changes your agents have finished, loops that stopped for a person, and checklist proposals waiting for a yes."
      />
      {isLoading && <p className="text-sm text-text-tertiary">Loading…</p>}
      {error && <Alert variant="danger">Could not load review loops: {errorMessage(error)}</Alert>}
      {data && groups.length === 0 && (
        <Card className="p-8 text-center" data-testid="loops-inbox-empty">
          <p className="mb-1 text-md font-semibold text-text-primary">Nothing needs you</p>
          <p className="mx-auto max-w-xl text-sm text-text-tertiary">
            When a change is ready to merge, a loop stops for a person or the checklist gets a proposal, it shows up here.
          </p>
        </Card>
      )}
      <div className="space-y-6">
        {groups.map((g) => (
          <section key={g.id} aria-labelledby={`loops-proj-${g.id}`} className="space-y-2">
            <h2 id={`loops-proj-${g.id}`} className="text-md font-semibold text-text-primary">
              <Link className="underline" to={projectLoopHref(g.id)}>{g.name}</Link>
              <span className="ml-2 text-sm font-normal text-text-tertiary">{g.items.length}</span>
            </h2>
            <Card className="p-0">
              <ul className="divide-y divide-[var(--border)]">
                {g.items.map((i, n) => (
                  <li key={`${i.kind}-${i.ticket_id ?? i.checklist_item_id ?? n}`} className="space-y-1 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge label={KIND_LABEL[i.kind] ?? i.kind} />
                      <Link className="text-sm font-medium text-brand underline" to={itemHref(i)}>
                        {i.title}
                      </Link>
                      {!i.can_act && <span className="text-xs text-text-tertiary">View only</span>}
                    </div>
                    <p className="text-sm text-text-secondary">{i.reason}</p>
                  </li>
                ))}
              </ul>
            </Card>
          </section>
        ))}
      </div>
      {total > shown && (
        <p role="status" className="mt-4 text-xs text-text-tertiary">
          Showing the newest {shown} of {total}.
        </p>
      )}
    </PageContainer>
  );
}
