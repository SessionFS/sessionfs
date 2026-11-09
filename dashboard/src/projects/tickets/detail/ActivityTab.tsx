import { Button, Checkbox, Textarea } from '../../../components/ui';
import { filterTimeline, type ActivityFilter, type TimelineItem } from './timeline';
import { Dot, TimelineRow } from './TimelineRow';

export type { ActivityFilter };

const FILTERS: { key: ActivityFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'comments', label: 'Comments' },
  { key: 'history', label: 'History' },
];

export interface Composer {
  draft: string;
  setDraft: (v: string) => void;
  canSendToJira: boolean;
  sendToJira: boolean;
  setSendToJira: (v: boolean) => void;
  submitting: boolean;
  disabled: boolean;
  onSubmit: () => void;
}

export function ActivityTab({
  items,
  filter,
  onFilter,
  onLoadEarlier,
  loadingEarlier,
  loadEarlierFailed,
  composer,
  stickyComposer,
  stickyBg = 'bg-overlay',
}: {
  items: TimelineItem[];
  filter: ActivityFilter;
  onFilter: (f: ActivityFilter) => void;
  onLoadEarlier: () => void;
  loadingEarlier: boolean;
  loadEarlierFailed: boolean;
  composer: Composer;
  /** Keep the composer in view at the bottom of a scrolling pane. */
  stickyComposer: boolean;
  /** Background behind the sticky composer: the pane's own. */
  stickyBg?: string;
}) {
  const shown = filterTimeline(items, filter);
  return (
    <section aria-label="Activity" className="text-sm">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div role="group" aria-label="Show" className="inline-flex rounded-md border border-border bg-surface p-0.5">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              aria-pressed={filter === f.key}
              onClick={() => onFilter(f.key)}
              className={`rounded px-2.5 py-1 text-xs font-medium outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
                filter === f.key ? 'bg-surface-hover text-text-primary' : 'text-text-secondary hover:text-text-primary'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <span className="text-2xs text-text-tertiary">Oldest first</span>
      </div>

      {shown.length > 0 ? (
        <ol className="relative ml-1.5 border-l border-border">
          {shown.map((item) =>
            item.kind === 'more' ? (
              <li key={item.key} className="relative py-1 pl-5">
                <Dot />
                {loadEarlierFailed && !loadingEarlier ? (
                  <span className="flex flex-wrap items-baseline gap-x-2 text-xs text-danger" role="alert">
                    Couldn't load earlier history.
                    <Button
                      variant="ghost"
                      size="sm"
                      className="!px-1.5 !py-0.5 !text-xs !font-normal !text-brand"
                      onClick={onLoadEarlier}
                    >
                      Try again
                    </Button>
                  </span>
                ) : (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="!-ml-1.5 !px-1.5 !py-0.5 !text-xs !font-normal !text-brand"
                    loading={loadingEarlier}
                    onClick={onLoadEarlier}
                  >
                    Show earlier history
                  </Button>
                )}
              </li>
            ) : (
              <TimelineRow key={item.key} item={item} />
            ),
          )}
        </ol>
      ) : (
        <p className="text-xs text-text-tertiary">
          {filter === 'comments' ? 'No comments yet.' : 'Nothing here yet.'}
        </p>
      )}

      {/* The composer ends the timeline. In a scrolling pane it stays in
          view while the thread scrolls above it. */}
      <div className={stickyComposer ? `sticky bottom-0 z-10 -mx-1 mt-3 ${stickyBg} px-1 pb-1 pt-2` : 'mt-3'}>
        <div className="rounded-lg border border-border bg-surface p-2 shadow-[var(--shadow-sm)]">
          <Textarea
            value={composer.draft}
            onChange={(e) => composer.setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && composer.draft.trim() && !composer.disabled) {
                e.preventDefault();
                composer.onSubmit();
              }
            }}
            placeholder="Add comment…"
            aria-label="Add comment"
            rows={2}
            className="min-h-0 !border-0 !bg-transparent !shadow-none focus-visible:!shadow-none"
          />
          <div className="flex flex-wrap items-center justify-between gap-2 px-1 pt-1">
            {composer.canSendToJira ? (
              <Checkbox
                checked={composer.sendToJira}
                onChange={(e) => composer.setSendToJira(e.target.checked)}
                label="Send to Jira"
                description="Also post this comment to the linked Jira issue."
              />
            ) : (
              <span />
            )}
            <Button
              variant={composer.draft.trim() ? 'primary' : 'secondary'}
              size="sm"
              disabled={!composer.draft.trim() || composer.disabled}
              loading={composer.submitting}
              onClick={composer.onSubmit}
            >
              Comment
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}
