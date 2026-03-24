import { useState } from 'react';
import Markdown from 'react-markdown';

interface BlockProps {
  block: Record<string, unknown>;
}

export default function ContentBlock({ block }: BlockProps) {
  const type = block.type as string;

  if (type === 'text') {
    return (
      <div className="prose prose-invert prose-sm max-w-none [&_pre]:bg-code-bg [&_pre]:border [&_pre]:border-border [&_pre]:rounded [&_code]:text-sm">
        <Markdown>{String(block.text || '')}</Markdown>
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
    return <span className="text-text-muted italic text-sm">[image reference]</span>;
  }

  if (type === 'summary') {
    return (
      <div className="text-text-muted italic text-sm border-l-2 border-border pl-3">
        {String(block.text || '')}
      </div>
    );
  }

  return (
    <pre className="text-sm text-text-muted bg-bg-secondary p-2 rounded overflow-x-auto">
      {JSON.stringify(block, null, 2)}
    </pre>
  );
}

function ToolUseBlock({ block }: BlockProps) {
  const [open, setOpen] = useState(false);
  const name = String(block.name || 'tool');
  const input = block.input as Record<string, unknown> | undefined;
  const inputStr = input ? JSON.stringify(input, null, 2) : '';
  const isLong = inputStr.length > 200;

  return (
    <div className="border border-border rounded bg-bg-secondary text-sm">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-bg-tertiary transition-colors"
      >
        <span className="text-role-tool font-mono">{name}</span>
        {typeof block.tool_use_id === 'string' && (
          <span className="text-text-muted font-mono">{block.tool_use_id.slice(0, 8)}</span>
        )}
        <span className="ml-auto text-text-muted">{open ? '\u25B2' : '\u25BC'}</span>
      </button>
      {(open || !isLong) && inputStr && (
        <pre className="px-3 py-2 border-t border-border overflow-x-auto text-text-secondary whitespace-pre-wrap">
          {inputStr}
        </pre>
      )}
    </div>
  );
}

function ToolResultBlock({ block }: BlockProps) {
  const [open, setOpen] = useState(false);
  const content = String(block.content || block.output || '');
  const lines = content.split('\n');
  const isLong = lines.length > 20;

  return (
    <div className="border border-border rounded bg-bg-secondary text-sm">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-bg-tertiary transition-colors"
      >
        <span className="text-text-muted">result</span>
        {isLong && (
          <span className="text-text-muted">({String(lines.length)} lines)</span>
        )}
        {isLong && (
          <span className="ml-auto text-text-muted">{open ? '\u25B2' : '\u25BC'}</span>
        )}
      </button>
      {(open || !isLong) && (
        <pre className="px-3 py-2 border-t border-border overflow-x-auto text-text-secondary whitespace-pre-wrap max-h-96 overflow-y-auto">
          {content}
        </pre>
      )}
    </div>
  );
}

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded bg-thinking-bg border border-border">
      <button
        onClick={() => setOpen(!open)}
        className="w-full px-3 py-1.5 text-left text-sm text-text-muted hover:text-text-secondary transition-colors flex items-center gap-2"
      >
        <span>Thinking</span>
        <span>{open ? '\u25B2' : '\u25BC'}</span>
      </button>
      {open && (
        <pre className="px-3 py-2 border-t border-border text-sm text-text-muted whitespace-pre-wrap max-h-80 overflow-y-auto">
          {text}
        </pre>
      )}
    </div>
  );
}
