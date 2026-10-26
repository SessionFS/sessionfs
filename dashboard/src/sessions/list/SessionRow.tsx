import type { SessionSummary } from '../../api/client';
import { fullToolName, isUnknownModelId } from '../../utils/models';
import { formatTokens } from '../../utils/tokens';
import RelativeDate from '../../components/RelativeDate';
import { TOOL_COLORS } from '../../utils/tools';
import { Button, Checkbox } from '../../components/ui';
import type { SessionSummaryWithAudit } from './sessionSort';
import { BookmarkDropdown } from './BookmarkDropdown';
import { ModelChip } from './ModelChip';

/* ── Individual session row with hover actions ── */

export function SessionRow({
  session: s,
  onClick,
  onResume,
  onNavigate,
  lineageBadge,
  isChild,
  isBookmarked,
  selectMode,
  selected,
  onToggleSelect,
}: {
  session: SessionSummary;
  onClick: () => void;
  onResume: () => void;
  onNavigate?: () => void;
  lineageBadge?: React.ReactNode;
  isChild?: boolean;
  isBookmarked?: boolean;
  selectMode?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  const titleClass = `font-semibold text-text-primary ${isChild ? 'text-sm' : 'text-md'}`;
  return (
    <div
      className={`group relative bg-surface border border-border rounded-lg hover:bg-bg-elevated hover:border-border-strong transition-[background-color,border-color] duration-150 ease-out ${isChild ? 'px-3 py-3' : 'px-4 py-[14px]'} ${selected ? 'ring-1 ring-brand bg-brand/5' : ''}`}
    >
      {/* Line 1: Title + hover actions + timestamp. The title button's ::after
          stretches over the whole row so the row stays one click target;
          every other control sits above it (relative z-10) as a sibling. */}
      <div className="flex items-center gap-3 mb-1">
        {selectMode && (
          <Checkbox
            aria-label={`Select ${s.title || 'untitled session'}`}
            checked={selected || false}
            onChange={() => onToggleSelect?.()}
            className="mr-3 shrink-0 relative z-10"
          />
        )}
        <button
          type="button"
          onClick={onClick}
          className={`${titleClass} min-w-0 flex-1 block text-left cursor-pointer outline-none after:absolute after:inset-0 after:rounded-lg focus-visible:after:shadow-[0_0_0_3px_var(--brand-glow)]`}
        >
          <span className="block line-clamp-2 break-words sm:line-clamp-1">
            {isChild && <span className="text-text-tertiary mr-1">&#8627;</span>}
            {s.title || <span className="text-text-tertiary italic">Untitled session</span>}
            {s.parent_session_id && !isChild && (
              <span className="text-text-tertiary text-xs ml-1.5">&crarr; fork</span>
            )}
          </span>
        </button>
        <div className="ml-auto flex items-center gap-2 shrink-0 relative z-10 pointer-events-none [&>*]:pointer-events-auto">
          {/* Row actions */}
          <div className="flex items-center gap-1.5">
            {onNavigate && (
              <Button
                variant="secondary"
                size="sm"
                onClick={onNavigate}
                className="!py-0.5 !px-2 font-medium opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
              >
                View
              </Button>
            )}
            <Button
              size="sm"
              onClick={onResume}
              className="!py-1 !px-2.5 shadow-none opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
            >
              Resume
            </Button>
          </div>
          {lineageBadge}
          <TrustBadge score={(s as SessionSummaryWithAudit).audit_trust_score} />
          <span className={`text-text-tertiary ${isChild ? 'text-xs' : 'text-sm'}`}>
            <RelativeDate iso={s.updated_at} />
          </span>
          <BookmarkDropdown sessionId={s.id} isBookmarked={isBookmarked} />
        </div>
      </div>
      {/* Line 2: tool name + session id as mono-chips; counts + model as text-tertiary */}
      <div className={`flex flex-wrap items-center gap-x-1.5 gap-y-1 ${isChild ? 'text-xs' : 'text-sm'} text-text-tertiary`}>
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{ backgroundColor: TOOL_COLORS[s.source_tool] || 'var(--text-tertiary)' }}
        />
        <span className="text-mono-chip whitespace-nowrap">{fullToolName(s.source_tool)}</span>
        <span className="text-mono-chip">{s.id.slice(0, 12)}</span>
        <span className="opacity-40">&middot;</span>
        <span className="tabular-nums whitespace-nowrap">{s.message_count} msgs</span>
        {!isChild && (s.total_input_tokens + s.total_output_tokens) > 0 && (
          <>
            <span className="opacity-40">&middot;</span>
            <span className="tabular-nums whitespace-nowrap">{formatTokens(s.total_input_tokens + s.total_output_tokens)}</span>
          </>
        )}
        {!isUnknownModelId(s.model_id) && (
          <>
            <span className="opacity-40">&middot;</span>
            <ModelChip modelId={s.model_id as string} />
          </>
        )}
      </div>
    </div>
  );
}

function TrustBadge({ score }: { score?: number | null }) {
  if (score == null) return null;
  const pct = Math.round(score * 100);
  const color =
    pct >= 90
      ? 'bg-success'
      : pct >= 70
        ? 'bg-warning'
        : 'bg-danger';
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full shrink-0 ${color}`}
      title={`Trust score: ${pct}%`}
    />
  );
}
