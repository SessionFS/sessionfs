import { useState } from 'react';
import Markdown from 'react-markdown';

interface BlockProps {
  block: Record<string, unknown>;
}

export default function ContentBlock({ block }: BlockProps) {
  const type = block.type as string;

  if (type === 'text') {
    return (
      <div className="prose prose-invert prose-sm max-w-[65ch] [&_pre]:bg-[var(--bg-sunken)] [&_pre]:border [&_pre]:border-[var(--border)] [&_pre]:rounded-lg [&_code]:text-sm">
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
    return <span className="text-[var(--text-tertiary)] italic text-sm">[image reference]</span>;
  }

  if (type === 'summary') {
    return (
      <div className="text-[var(--text-tertiary)] italic text-sm border-l-2 border-[var(--border)] pl-3">
        {String(block.text || '')}
      </div>
    );
  }

  return (
    <pre className="text-sm text-[var(--text-tertiary)] bg-[var(--bg-sunken)] border border-[var(--border)] p-2 rounded-lg overflow-x-auto">
      {JSON.stringify(block, null, 2)}
    </pre>
  );
}

function ToolUseBlock({ block }: BlockProps) {
  const [open, setOpen] = useState(false);
  const name = String(block.name || 'tool');
  const input = block.input as Record<string, unknown> | undefined;
  const inputStr = input ? JSON.stringify(input, null, 2) : '';

  return (
    <div className="ml-2">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-left hover:opacity-80 transition-opacity"
      >
        <span className="text-[var(--text-tertiary)] text-xs">{open ? '\u25BC' : '\u25B6'}</span>
        <span className="font-mono text-sm text-[var(--text-secondary)]">Tool: {name}</span>
      </button>
      {open && inputStr && (
        <pre className="mt-1 bg-[var(--bg-sunken)] border border-[var(--border)] rounded-lg p-3 font-mono text-xs text-[var(--text-secondary)] overflow-x-auto whitespace-pre-wrap">
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

  return (
    <div className="ml-4 opacity-80">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-left hover:opacity-80 transition-opacity"
      >
        <span className="text-[var(--text-tertiary)] text-xs">{open ? '\u25BC' : '\u25B6'}</span>
        <span className="font-mono text-xs text-[var(--text-tertiary)]">Result ({String(lines.length)} lines)</span>
      </button>
      {open && (
        <pre className="mt-1 bg-[var(--bg-sunken)] border border-[var(--border)] rounded-lg p-3 font-mono text-xs text-[var(--text-tertiary)] overflow-x-auto whitespace-pre-wrap max-h-96 overflow-y-auto">
          {content}
        </pre>
      )}
    </div>
  );
}

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg bg-[var(--bg-sunken)] border border-[var(--border)]">
      <button
        onClick={() => setOpen(!open)}
        className="w-full px-3 py-1.5 text-left text-sm text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors flex items-center gap-2"
      >
        <span>Thinking</span>
        <span>{open ? '\u25B2' : '\u25BC'}</span>
      </button>
      {open && (
        <pre className="px-3 py-2 border-t border-[var(--border)] text-sm text-[var(--text-tertiary)] whitespace-pre-wrap max-h-80 overflow-y-auto">
          {text}
        </pre>
      )}
    </div>
  );
}
