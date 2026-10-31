import { Alert, Card, TabPanel, Tabs } from '../../components/ui';
import { useLoopInsights, useLoops } from '../../hooks/useReviewLoop';
import { errorMessage } from '../../utils/errorMessage';
import { ChecklistPanel } from './LoopActions';
import { ActiveLoops, Caught, CostTrend, Learning, NeedsYou, Scoreboard } from './Panels';
import { ShipReports } from './ShipReport';
import { LoopBoard } from './LoopBoard';
import { StoryView } from './StoryView';
import { DOCS_URL } from './format';

export interface ReviewLoopTabProps {
  projectId: string;
  /** Open story (URL `story=`), or null for Mission Control. */
  storyId: string | null;
  onOpenStory: (ticketId: string | null) => void;
  /** Editors may raise limits, close items and manage the checklist. */
  canEdit?: boolean;
  /** `board` shows the Loop Board, `ship` the Ship Reports (URL `view=`); anything else is Mission Control. */
  view?: string | null;
  onChangeView?: (view: 'home' | 'board' | 'ship') => void;
}

export function LoopEmptyState() {
  return (
    <Card className="p-8 text-center" data-testid="loop-empty">
      <p className="mb-1 text-md font-semibold text-text-primary">No review loops yet</p>
      <p className="mx-auto max-w-xl text-sm text-text-tertiary">
        A Review Loop has your agents write the change, a different agent review it, and the findings feed back until the reviewer finds nothing.
        SessionFS records every round, shows what was caught and what it cost, and only calls a change ready when a trusted reviewer has cleared the exact commit.
      </p>
      <p className="mt-4 text-sm text-text-secondary">
        Start one with <code className="font-mono text-text-primary">sfs loop start &lt;ticket&gt;</code>{' '}
        <span className="text-text-tertiary">(coming soon)</span>.{' '}
        <a className="text-brand underline" href={DOCS_URL} target="_blank" rel="noopener noreferrer">
          Read how it works{" "}<span className="sr-only">(opens in a new tab)</span>
        </a>
      </p>
    </Card>
  );
}

/**
 * Review Loop → Mission Control. Everything here is a count of stored
 * records; nothing is estimated. Harm is the only loud colour.
 */
export default function ReviewLoopTab({ projectId, storyId, onOpenStory, canEdit = false, view = null, onChangeView }: ReviewLoopTabProps) {
  const loops = useLoops(projectId);
  const insights = useLoopInsights(projectId);

  if (storyId) {
    return <StoryView key={storyId} projectId={projectId} ticketId={storyId} loops={loops.data ?? []} onBack={() => onOpenStory(null)} canEdit={canEdit} />;
  }

  if (loops.isLoading) return <p className="p-4 text-text-tertiary">Loading Review Loop…</p>;
  if (loops.error) {
    return <Alert variant="danger">Could not load Review Loop: {errorMessage(loops.error)}</Alert>;
  }

  const data = loops.data ?? [];
  const board = view === 'board';
  const current: 'home' | 'board' | 'ship' = board ? 'board' : view === 'ship' ? 'ship' : 'home';
  const ins = insights.data;
  const hasAnything =
    data.some((l) => l.children.length > 0) ||
    (ins && (ins.caught.length > 0 || ins.learning.length > 0 || ins.cost_per_accepted_change.length > 0));

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-lg font-semibold text-text-primary">Review Loop</h2>
        <p className="text-sm text-text-tertiary">
          What your agents wrote, what the reviewers caught, and what it cost.
        </p>
      </div>
      {!hasAnything ? (
        <LoopEmptyState />
      ) : (
        <>
          <NeedsYou projectId={projectId} loops={data} canEdit={canEdit} />
          <Tabs
            bare
            variant="underline"
            idPrefix="loop-view"
            aria-label="Review Loop view"
            activeKey={current}
            onChange={(k) => onChangeView?.(k === 'board' ? 'board' : k === 'ship' ? 'ship' : 'home')}
            tabs={[
              { key: 'home', label: 'Overview' },
              { key: 'board', label: 'Board' },
              { key: 'ship', label: 'Ship Reports' },
            ]}
          />
          <TabPanel idPrefix="loop-view" tabKey={current} className="space-y-8 outline-none">
            {board ? (
              <LoopBoard projectId={projectId} loops={data} />
            ) : current === 'ship' ? (
              <ShipReports projectId={projectId} canEdit={canEdit} />
            ) : (
              <>
                <Caught projectId={projectId} caught={ins?.caught ?? []} total={ins?.caught_total} />
                <div className="grid gap-8 lg:grid-cols-2">
                  <CostTrend points={ins?.cost_per_accepted_change ?? []} />
                  <Learning classes={ins?.learning ?? []} />
                </div>
                <Scoreboard rows={ins?.scoreboard ?? []} />
                {data.some((l) => l.children.length > 0) && <ActiveLoops projectId={projectId} loops={data} />}
                <ChecklistPanel projectId={projectId} canEdit={canEdit} />
              </>
            )}
          </TabPanel>
        </>
      )}
      {insights.error && (
        <p role="status" className="text-xs text-text-tertiary">
          The rollups could not be loaded: {errorMessage(insights.error)}
        </p>
      )}
    </div>
  );
}
