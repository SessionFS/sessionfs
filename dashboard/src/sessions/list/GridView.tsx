import type { SessionSummary } from '../../api/client';
import { abbreviateModel, fullToolName } from '../../utils/models';
import { formatTokens } from '../../utils/tokens';
import RelativeDate from '../../components/RelativeDate';
import { TOOL_COLORS } from '../../utils/tools';
import { Button } from '../../components/ui';
import { BookmarkDropdown } from './BookmarkDropdown';

/* ── Grid-grouped session section ── */

export function GridGroup({
  label,
  sessions,
  isFirst,
  onRowClick,
  onResume,
  isInFolder,
}: {
  label: string;
  sessions: SessionSummary[];
  isFirst?: boolean;
  onRowClick: (id: string) => void;
  onResume: (s: SessionSummary) => void;
  isInFolder?: boolean;
}) {
  if (sessions.length === 0) return null;

  return (
    <div className={`mb-5 ${isFirst ? 'mt-0' : 'mt-8'}`}>
      <h2 className="text-micro font-semibold uppercase text-text-tertiary pt-3 pb-2 mb-3 border-b border-border col-span-full">
        {label}
      </h2>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {sessions.map((s) => (
          <SessionGridCard
            key={s.id}
            session={s}
            onClick={() => onRowClick(s.id)}
            onResume={() => onResume(s)}
            isBookmarked={!!isInFolder}
          />
        ))}
      </div>
    </div>
  );
}

/* ── Grid-view session card ── */

function SessionGridCard({
  session: s,
  onClick,
  onResume,
  isBookmarked,
}: {
  session: SessionSummary;
  onClick: () => void;
  onResume: () => void;
  isBookmarked?: boolean;
}) {
  const toolColor = TOOL_COLORS[s.source_tool] || 'var(--text-tertiary)';

  return (
    <div
      role="button"
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter') onClick(); }}
      tabIndex={0}
      className="group bg-surface border border-border rounded-lg cursor-pointer hover:bg-bg-elevated hover:border-border-strong transition-[background-color,border-color] duration-150 ease-out outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] flex flex-col"
    >
      {/* Body */}
      <div className="px-4 pt-[14px] pb-2 flex-1 min-w-0">
        <h4 className="text-md font-semibold text-text-primary truncate mb-1.5">
          {s.title || <span className="text-text-tertiary italic">Untitled session</span>}
        </h4>
        <div className="flex flex-wrap items-center gap-1 text-xs text-text-tertiary">
          <span
            className="w-2 h-2 rounded-full shrink-0"
            style={{ backgroundColor: toolColor }}
          />
          <span className="text-mono-chip">{fullToolName(s.source_tool)}</span>
          <span className="text-mono-chip">{s.id.slice(0, 12)}</span>
          <span className="opacity-40">&middot;</span>
          <span className="tabular-nums">{s.message_count} msgs</span>
          {(s.total_input_tokens + s.total_output_tokens) > 0 && (
            <>
              <span className="opacity-40">&middot;</span>
              <span className="tabular-nums">{formatTokens(s.total_input_tokens + s.total_output_tokens)}</span>
            </>
          )}
          {s.model_id && s.model_id !== '<synthetic>' && (
            <>
              <span className="opacity-40">&middot;</span>
              <span>{abbreviateModel(s.model_id)}</span>
            </>
          )}
        </div>
      </div>
      {/* Footer */}
      <div className="px-4 pb-3 flex items-center justify-between">
        <span className="text-xs text-text-tertiary">
          <RelativeDate iso={s.updated_at} />
        </span>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            onClick={(e) => { e.stopPropagation(); onResume(); }}
            className="!py-1 !px-2.5 shadow-none opacity-0 group-hover:opacity-100 group-focus:opacity-100 focus-visible:opacity-100"
          >
            Resume
          </Button>
          <div onClick={(e) => e.stopPropagation()}>
            <BookmarkDropdown sessionId={s.id} isBookmarked={isBookmarked} />
          </div>
        </div>
      </div>
    </div>
  );
}
