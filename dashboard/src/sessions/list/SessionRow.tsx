import type { SessionSummary } from '../../api/client';
import { abbreviateModel, fullToolName } from '../../utils/models';
import { formatTokens } from '../../utils/tokens';
import RelativeDate from '../../components/RelativeDate';
import { TOOL_COLORS } from '../../utils/tools';
import { Button, Checkbox } from '../../components/ui';
import type { SessionSummaryWithAudit } from './sessionSort';
import { BookmarkDropdown } from './BookmarkDropdown';

/* ── Individual session row with hover actions ── */

export function SessionRow({
  session: s,
  onClick,
  onKeyDown,
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
  onKeyDown: (e: React.KeyboardEvent) => void;
  onResume: () => void;
  onNavigate?: () => void;
  lineageBadge?: React.ReactNode;
  isChild?: boolean;
  isBookmarked?: boolean;
  selectMode?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  return (
    <div
      role="button"
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === ' ') { e.preventDefault(); onClick(); } else { onKeyDown(e); } }}
      tabIndex={0}
      className={`group bg-surface border border-border rounded-lg cursor-pointer hover:bg-bg-elevated hover:border-border-strong transition-[background-color,border-color] duration-150 ease-out outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${isChild ? 'px-3 py-3' : 'px-4 py-[14px]'} ${selected ? 'ring-1 ring-brand bg-brand/5' : ''}`}
    >
      {/* Line 1: Title + hover actions + timestamp */}
      <div className="flex items-center gap-3 mb-1">
        {selectMode && (
          <Checkbox
            aria-label={`Select ${s.title || 'untitled session'}`}
            checked={selected || false}
            onChange={() => onToggleSelect?.()}
            className="mr-3 shrink-0"
            onClick={(e) => e.stopPropagation()}
          />
        )}
        <span
          className={`font-semibold text-text-primary min-w-0 flex-1 line-clamp-2 break-words sm:line-clamp-1 ${isChild ? 'text-sm' : 'text-md'} ${onNavigate ? 'cursor-pointer' : ''}`}
          onClick={onNavigate ? (e) => { e.stopPropagation(); onNavigate(); } : undefined}
        >
          {isChild && <span className="text-text-tertiary mr-1">&#8627;</span>}
          {s.title || <span className="text-text-tertiary italic">Untitled session</span>}
          {s.parent_session_id && !isChild && (
            <span className="text-text-tertiary text-xs ml-1.5">&crarr; fork</span>
          )}
        </span>
        <div className="ml-auto flex items-center gap-2 shrink-0">
          {/* Row actions */}
          <div className="flex items-center gap-1.5">
            {onNavigate && (
              <Button
                variant="secondary"
                size="sm"
                onClick={(e) => { e.stopPropagation(); onNavigate(); }}
                className="!py-0.5 !px-2 font-medium opacity-0 group-hover:opacity-100"
              >
                View
              </Button>
            )}
            <Button
              size="sm"
              onClick={(e) => { e.stopPropagation(); onResume(); }}
              className="!py-1 !px-2.5 shadow-none opacity-0 group-hover:opacity-100 group-focus:opacity-100 focus-visible:opacity-100"
            >
              Resume
            </Button>
          </div>
          {lineageBadge}
          <TrustBadge score={(s as SessionSummaryWithAudit).audit_trust_score} />
          <span className={`text-text-tertiary ${isChild ? 'text-xs' : 'text-sm'}`}>
            <RelativeDate iso={s.updated_at} />
          </span>
          <div onClick={(e) => e.stopPropagation()}>
            <BookmarkDropdown sessionId={s.id} isBookmarked={isBookmarked} />
          </div>
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
        {s.model_id && s.model_id !== '<synthetic>' && (
          <>
            <span className="opacity-40">&middot;</span>
            <span className="whitespace-nowrap">{abbreviateModel(s.model_id)}</span>
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
