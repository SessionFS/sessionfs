import ContentBlock from './ContentBlock';
import RelativeDate from '../components/RelativeDate';

interface MessageProps {
  message: Record<string, unknown>;
}

const ROLE_BADGE_STYLES: Record<string, string> = {
  user: 'border border-[var(--brand)] text-[var(--brand)] bg-transparent',
  assistant: 'border border-[var(--accent)] text-[var(--accent)] bg-transparent',
  tool: 'border border-[var(--text-tertiary)] text-[var(--text-tertiary)] bg-transparent',
  system: 'border border-[var(--text-tertiary)] text-[var(--text-tertiary)] bg-transparent',
  developer: 'border border-[var(--text-tertiary)] text-[var(--text-tertiary)] bg-transparent',
};

export default function MessageBlock({ message }: MessageProps) {
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

  const isUser = role === 'user';
  const isTool = role === 'tool';
  const badgeStyle = ROLE_BADGE_STYLES[role] || 'border border-[var(--text-tertiary)] text-[var(--text-tertiary)] bg-transparent';
  const borderClass = isUser ? 'border-l-[3px] border-l-[var(--brand)] pl-4' : '';
  const bgClass = isTool ? 'bg-[var(--bg-sunken)]' : 'bg-[var(--surface)]';

  return (
    <div className={`${borderClass} ${bgClass} rounded-lg p-3`}>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <span className={`text-xs font-medium rounded-full px-2 py-0.5 ${badgeStyle}`}>
            {role === 'assistant' ? 'Assistant' : role === 'user' ? 'User' : role === 'system' ? 'System' : role.charAt(0).toUpperCase() + role.slice(1)}
          </span>
          {model && <span className="text-[var(--text-tertiary)] text-xs font-mono">{model}</span>}
        </div>
        {timestamp && (
          <span className="text-xs text-[var(--text-tertiary)]">
            <RelativeDate iso={timestamp} />
          </span>
        )}
      </div>
      <div className="flex flex-col gap-2">
        {blocks.map((block, i) => (
          <ContentBlock key={i} block={block} />
        ))}
      </div>
    </div>
  );
}
