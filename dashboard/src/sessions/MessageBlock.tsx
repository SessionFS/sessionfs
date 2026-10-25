import ContentBlock from './ContentBlock';
import RelativeDate from '../components/RelativeDate';
import { messageAnchorId } from './messageAnchor';

interface MessageProps {
  message: Record<string, unknown>;
  /** 0-based messages.jsonl line number; gives the block a stable `msg-<n>` id. */
  index?: number;
  /** Briefly ring the block (the target of a `?msg=` deep link). */
  highlighted?: boolean;
}

// ── Role rails — 2px left border, one colour per role ──────────────

const ROLE_RAIL: Record<string, string> = {
  user: 'border-l-[2px] border-l-[var(--brand)]',
  assistant: 'border-l-[2px] border-l-[var(--accent)]',
  tool: 'border-l-[2px] border-l-[var(--text-tertiary)]',
  system: 'border-l-[2px] border-l-[var(--warning)]',
  developer: 'border-l-[2px] border-l-[var(--text-tertiary)]',
};

// ── Role labels — small uppercase mono, readable but restrained ────

const ROLE_LABEL: Record<string, string> = {
  user: 'User',
  assistant: 'Assistant',
  tool: 'Tool',
  system: 'System',
  developer: 'Developer',
};

function roleLabelClass(role: string): string {
  if (role === 'assistant') return 'text-2xs font-mono uppercase tracking-wider text-[var(--accent)]';
  if (role === 'user') return 'text-2xs font-mono uppercase tracking-wider text-[var(--brand)]';
  return 'text-2xs font-mono uppercase tracking-wider text-text-tertiary';
}

// ── MessageBlock ───────────────────────────────────────────────────

export default function MessageBlock({ message, index, highlighted = false }: MessageProps) {
  const role = String(message.role || 'unknown');
  const content = message.content as Record<string, unknown>[] | string | undefined;
  const timestamp = message.timestamp as string | undefined;
  const model = message.model as string | undefined;
  const isSidechain = message.is_sidechain as boolean | undefined;

  if (isSidechain) return null; // Skip sidechain messages in main view

  const blocks: Record<string, unknown>[] = [];
  if (typeof content === 'string') {
    blocks.push({ type: 'text', text: content });
  } else if (Array.isArray(content)) {
    blocks.push(...content);
  }

  const rail = ROLE_RAIL[role] || 'border-l-[2px] border-l-[var(--text-tertiary)]';
  const label = ROLE_LABEL[role] || role.charAt(0).toUpperCase() + role.slice(1);
  const isTool = role === 'tool';
  const isSystem = role === 'system';

  // Tool + system get sunken background; user + assistant stay on surface
  const bgClass = isTool || isSystem ? 'bg-bg-sunken/60' : 'bg-surface';

  return (
    <div
      id={index != null ? messageAnchorId(index) : undefined}
      data-message-index={index}
      data-highlighted={highlighted || undefined}
      tabIndex={index != null ? -1 : undefined}
      className={`${rail} ${bgClass} rounded-lg px-4 py-3 scroll-mt-4 outline-none motion-safe:transition-shadow motion-safe:duration-500 ${
        highlighted ? 'shadow-[0_0_0_2px_var(--brand),0_0_0_6px_var(--brand-glow)]' : ''
      }`}
    >
      {/* Top bar — role label, model, timestamp */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className={roleLabelClass(role)}>{label}</span>
          {model && (
            <span className="text-2xs font-mono text-text-tertiary">{model}</span>
          )}
        </div>
        {timestamp && (
          <span className="text-2xs text-text-tertiary tabular-nums">
            <RelativeDate iso={timestamp} />
          </span>
        )}
      </div>

      {/* Content blocks — tight spacing within a turn */}
      <div className="flex flex-col gap-2">
        {blocks.map((block, i) => (
          <ContentBlock key={i} block={block} />
        ))}
      </div>
    </div>
  );
}
