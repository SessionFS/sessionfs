import { useState, useCallback } from 'react';
import { Button } from '../../components/ui';
import type { BlockProps } from './types';
import { ellipsis, looksLikeError } from './text';
import { ChevronIcon } from './icons';
import { CopyButton } from './CopyButton';

export function ToolResultBlock({ block }: BlockProps) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const content = String(block.content ?? block.output ?? '');
  const isError = Boolean(block.is_error) || looksLikeError(content);
  const lines = content.split('\n');
  const lineCount = lines.length;
  const isLong = lineCount > 12;

  const toggle = useCallback(() => setOpen((o) => !o), []);

  return (
    <div className={`rounded-lg border ${isError ? 'border-[color-mix(in_srgb,var(--danger)_30%,transparent)]' : 'border-border'} bg-bg-sunken/60`}>
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
        aria-label={`Tool result${isError ? ' (error)' : ''} — ${lineCount} lines`}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-bg-sunken transition-colors duration-150 rounded-lg outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        <span className="text-text-tertiary shrink-0">
          <ChevronIcon open={open} />
        </span>
        {isError ? (
          <span className="text-2xs font-mono text-[var(--danger)] font-medium">Error</span>
        ) : (
          <span className="text-2xs font-mono text-text-tertiary">Result</span>
        )}
        <span className="text-xs text-text-tertiary">
          {lineCount} {lineCount === 1 ? 'line' : 'lines'}
        </span>
        {!open && (
          <span className="text-xs text-text-tertiary truncate max-w-64">
            {ellipsis(lines[0] || '', 80)}
          </span>
        )}
      </button>

      {open && (
        <div className="border-t border-border">
          <div className={`${isError ? 'bg-[color-mix(in_srgb,var(--danger)_6%,transparent)]' : ''}`}>
            <div className="flex items-center justify-between px-3 py-1 border-b border-border">
              <span className={`text-2xs font-mono ${isError ? 'text-[var(--danger)]' : 'text-text-tertiary'}`}>
                {isError ? 'Error output' : 'Output'}
              </span>
              <CopyButton text={content} label="Copy output" />
            </div>
            <pre
              className={`px-3 py-2 text-xs font-mono overflow-x-auto whitespace-pre-wrap ${
                isError ? 'text-[var(--danger)]' : 'text-text-secondary'
              } ${!expanded && isLong ? 'line-clamp-[12]' : ''} ${isLong ? 'max-h-80 overflow-y-auto' : ''}`}
            >
              {content || '(empty)'}
            </pre>
          </div>
          {isLong && (
            <div className="px-3 py-1.5 border-t border-border">
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
                className="!px-1.5 !py-0.5 font-normal text-text-tertiary"
              >
                {expanded ? 'Show less' : `Show full output (${lineCount} lines)`}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
