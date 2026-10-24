import Markdown from 'react-markdown';
import type { BlockProps } from './content/types';
import { markdownComponents } from './content/markdownComponents';
import { ToolUseBlock } from './content/ToolUseBlock';
import { ToolResultBlock } from './content/ToolResultBlock';
import { ThinkingBlock } from './content/ThinkingBlock';

// ── Main ContentBlock Dispatcher ───────────────────────────────────

export default function ContentBlock({ block }: BlockProps) {
  const type = block.type as string;

  if (type === 'text') {
    return (
      <div className="max-w-[72ch] text-sm text-text-secondary">
        <Markdown components={markdownComponents}>
          {String(block.text || '')}
        </Markdown>
      </div>
    );
  }

  if (type === 'tool_use') {
    return <ToolUseBlock block={block} />;
  }

  if (type === 'tool_result') {
    return <ToolResultBlock block={block} />;
  }

  if (type === 'thinking') {
    return <ThinkingBlock text={String(block.thinking || block.text || '')} />;
  }

  if (type === 'image') {
    const src = block.source as Record<string, unknown> | undefined;
    if (src?.type === 'base64') {
      return (
        <img
          src={`data:${src.media_type};base64,${src.data}`}
          alt="Session image"
          className="max-w-md rounded border border-border"
        />
      );
    }
    return <span className="text-text-tertiary italic text-sm">[image reference]</span>;
  }

  if (type === 'summary') {
    return (
      <div className="text-text-tertiary italic text-sm border-l-2 border-border pl-3">
        {String(block.text || '')}
      </div>
    );
  }

  return (
    <pre className="text-sm text-text-tertiary bg-bg-sunken border border-border p-2 rounded-lg overflow-x-auto">
      {JSON.stringify(block, null, 2)}
    </pre>
  );
}
