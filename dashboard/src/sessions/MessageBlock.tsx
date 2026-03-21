import ContentBlock from './ContentBlock';
import RelativeDate from '../components/RelativeDate';

interface MessageProps {
  message: Record<string, unknown>;
}

const ROLE_STYLES: Record<string, string> = {
  user: 'border-role-user/30 bg-role-user/5',
  assistant: 'border-role-assistant/30 bg-role-assistant/5',
  tool: 'border-role-tool/30 bg-role-tool/5',
  system: 'border-role-system/30 bg-role-system/5',
  developer: 'border-role-system/30 bg-role-system/5',
};

const ROLE_LABEL_STYLES: Record<string, string> = {
  user: 'text-role-user',
  assistant: 'text-role-assistant',
  tool: 'text-role-tool',
  system: 'text-role-system',
  developer: 'text-role-system',
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

  const style = ROLE_STYLES[role] || 'border-border';
  const labelStyle = ROLE_LABEL_STYLES[role] || 'text-text-secondary';

  return (
    <div className={`border-l-2 ${style} pl-4 py-2`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-xs font-medium uppercase tracking-wide ${labelStyle}`}>
          {role}
        </span>
        {timestamp && (
          <span className="text-text-muted text-xs">
            <RelativeDate iso={timestamp} />
          </span>
        )}
        {model && <span className="text-text-muted text-xs font-mono">{model}</span>}
      </div>
      <div className="flex flex-col gap-2">
        {blocks.map((block, i) => (
          <ContentBlock key={i} block={block} />
        ))}
      </div>
    </div>
  );
}
