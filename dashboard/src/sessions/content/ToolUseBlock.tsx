import { useState, useCallback } from 'react';
import type { BlockProps } from './types';
import { ellipsis } from './text';
import { describeToolUse } from './toolSummary';
import { ChevronIcon } from './icons';
import { CopyButton } from './CopyButton';
import { FileToolDetail, EditToolDetail } from './FileDetails';
import { BashToolDetail, SearchToolDetail, WebToolDetail } from './CommandDetails';
import { TodoToolDetail, AgentToolDetail, UnknownToolDetail } from './TaskDetails';

export function ToolUseBlock({ block }: BlockProps) {
  const [open, setOpen] = useState(false);
  const name = String(block.name || 'tool');
  const input = (block.input as Record<string, unknown>) ?? {};
  const { icon, summary } = describeToolUse(name, input);

  const toggle = useCallback(() => setOpen((o) => !o), []);

  return (
    <div className="rounded-lg border border-border bg-bg-sunken/60">
      {/* Row is a flex container, NOT a button — the expand toggle and the
          copy action are SIBLINGS so we never nest one <button> inside another
          (invalid HTML + React hydration error). */}
      <div className="flex items-center group rounded-lg hover:bg-bg-sunken transition-colors duration-150">
        <button
          type="button"
          onClick={toggle}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              toggle();
            }
          }}
          aria-expanded={open}
          aria-label={`${name}: ${summary}`}
          className="flex-1 min-w-0 flex items-center gap-2 px-3 py-1.5 text-left rounded-lg outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          <span className="text-text-tertiary shrink-0">
            <ChevronIcon open={open} />
          </span>
          <span className="text-text-tertiary shrink-0">{icon}</span>
          <span className="font-mono text-xs text-text-secondary whitespace-nowrap">{name}</span>
          <span className="text-sm text-text-tertiary truncate">{ellipsis(summary, 100)}</span>
        </button>
        {name === 'Bash' && !!input.command && (
          <span className="shrink-0 pr-3 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity duration-150">
            <CopyButton text={String(input.command)} label="Copy command" />
          </span>
        )}
      </div>

      {open && <ToolUseDetail name={name} input={input} />}
    </div>
  );
}

const KNOWN_TOOLS_WITH_DETAIL = new Set([
  'Read', 'Write', 'Edit', 'MultiEdit', 'Bash', 'Grep', 'Glob',
  'WebFetch', 'WebSearch', 'TodoWrite', 'Task', 'TaskOutput', 'Agent',
]);

export function ToolUseDetail({ name, input }: { name: string; input: Record<string, unknown> }) {
  if (!KNOWN_TOOLS_WITH_DETAIL.has(name)) {
    return <UnknownToolDetail input={input} />;
  }

  switch (name) {
    case 'Read':
    case 'Write':
      return <FileToolDetail input={input} />;
    case 'Edit':
    case 'MultiEdit':
      return <EditToolDetail name={name} input={input} />;
    case 'Bash':
      return <BashToolDetail input={input} />;
    case 'Grep':
    case 'Glob':
      return <SearchToolDetail input={input} />;
    case 'WebFetch':
    case 'WebSearch':
      return <WebToolDetail input={input} />;
    case 'TodoWrite':
      return <TodoToolDetail input={input} />;
    case 'Task':
    case 'TaskOutput':
    case 'Agent':
      return <AgentToolDetail input={input} />;
  }
}
