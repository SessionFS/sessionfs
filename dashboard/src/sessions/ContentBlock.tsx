import { useState, useCallback, type ReactNode } from 'react';
import Markdown from 'react-markdown';

// ── Types ──────────────────────────────────────────────────────────

interface BlockProps {
  block: Record<string, unknown>;
}

interface ToolUseInfo {
  icon: ReactNode;
  summary: string;
}

interface DiffLine {
  type: 'same' | 'removed' | 'added';
  text: string;
}

// ── Tool Icons (16×16, currentColor) ───────────────────────────────

function TerminalIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2.5" width="13" height="11" rx="2" />
      <path d="M4.5 5.5l2 2-2 2M8 9.5h3.5" />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 1.5H4a1.5 1.5 0 0 0-1.5 1.5v10A1.5 1.5 0 0 0 4 14.5h8a1.5 1.5 0 0 0 1.5-1.5V6L9 1.5Z" />
      <path d="M9 1.5V6h4.5" />
    </svg>
  );
}

function EditIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11.5 1.5l3 3-9.5 9.5L1.5 14.5l.5-3.5 9.5-9.5Z" />
      <path d="M10 3l3 3" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.5 10.5L14.5 14.5" />
    </svg>
  );
}

function GlobeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6.5" />
      <ellipse cx="8" cy="8" rx="3" ry="6.5" />
      <path d="M1.5 8h13M8 1.5v13" />
    </svg>
  );
}

function TodoIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2" width="13" height="12" rx="1.5" />
      <path d="M4.5 6.5l2 2 4.5-4.5" />
    </svg>
  );
}

function BotIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="5" width="10" height="8" rx="1.5" />
      <path d="M6 3.5V5M10 3.5V5" />
      <circle cx="8" cy="1.5" r="1" />
      <path d="M6 9h4M6 11h2.5" />
    </svg>
  );
}

function DefaultToolIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12.5 3.5a4 4 0 0 0-5.66 5.34l-5.34 2.66 1 2 2.66-1.34a4 4 0 0 0 5.34-5.66l-2 2a.5.5 0 0 1-.7-.7l2-2Z" />
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`transition-transform duration-150 ${open ? 'rotate-90' : 'rotate-0'}`}
    >
      <path d="M4.5 2.5L8 6l-3.5 3.5" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="7" height="7" rx="1" />
      <path d="M2.5 8H2a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v.5" />
    </svg>
  );
}

// ── Tool Summary Helper ────────────────────────────────────────────

function ellipsis(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + '…';
}

function describeToolUse(name: string, input: Record<string, unknown> | undefined): ToolUseInfo {
  const inp = input ?? {};

  switch (name) {
    case 'Read': {
      const fp = String(inp.file_path ?? '');
      return { icon: <FileIcon />, summary: fp || 'Read file' };
    }
    case 'Write': {
      const fp = String(inp.file_path ?? '');
      return { icon: <FileIcon />, summary: fp || 'Write file' };
    }
    case 'Edit': {
      const fp = String(inp.file_path ?? '');
      return { icon: <EditIcon />, summary: fp ? `${fp} — edit` : 'Edit' };
    }
    case 'MultiEdit': {
      const fp = String(inp.file_path ?? '');
      return { icon: <EditIcon />, summary: fp ? `${fp} — edit` : 'MultiEdit' };
    }
    case 'Bash': {
      const cmd = String(inp.command ?? '');
      return { icon: <TerminalIcon />, summary: cmd || 'Bash' };
    }
    case 'Grep': {
      const pattern = String(inp.pattern ?? '');
      const path = inp.path ? String(inp.path) : '';
      return { icon: <SearchIcon />, summary: path ? `${pattern} in ${path}` : pattern || 'Grep' };
    }
    case 'Glob': {
      const pattern = String(inp.pattern ?? '');
      const path = inp.path ? String(inp.path) : '';
      return { icon: <SearchIcon />, summary: path ? `${pattern} in ${path}` : pattern || 'Glob' };
    }
    case 'WebFetch': {
      const url = String(inp.url ?? '');
      return { icon: <GlobeIcon />, summary: url || 'WebFetch' };
    }
    case 'WebSearch': {
      const query = String(inp.query ?? '');
      return { icon: <GlobeIcon />, summary: query || 'WebSearch' };
    }
    case 'TodoWrite': {
      const todos = inp.todos as Array<Record<string, unknown>> | undefined;
      const count = todos?.length ?? 0;
      return { icon: <TodoIcon />, summary: count === 1 ? '1 todo' : `${count} todos` };
    }
    case 'Task': {
      const desc = String(inp.description ?? inp.subagent_type ?? '');
      return { icon: <BotIcon />, summary: desc || 'Task' };
    }
    case 'TaskOutput': {
      const taskId = String(inp.task_id ?? '');
      return { icon: <BotIcon />, summary: taskId || 'Task output' };
    }
    case 'Agent': {
      const desc = String(inp.description ?? inp.prompt ?? '');
      return { icon: <BotIcon />, summary: desc || 'Agent' };
    }
    default: {
      const keys = Object.keys(inp).slice(0, 3);
      const summary = keys.length > 0 ? keys.join(', ') : name;
      return { icon: <DefaultToolIcon />, summary };
    }
  }
}

// ── Line Diff for Edit ─────────────────────────────────────────────

function computeLineDiff(oldStr: string, newStr: string): DiffLine[] {
  const oldLines = oldStr.split('\n');
  const newLines = newStr.split('\n');
  const m = oldLines.length;
  const n = newLines.length;

  // Guard: if either is huge, fall back to simple split view
  if (m * n > 250_000) {
    const result: DiffLine[] = [];
    for (const l of oldLines) result.push({ type: 'removed', text: l });
    for (const l of newLines) result.push({ type: 'added', text: l });
    return result;
  }

  // LCS table
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (oldLines[i - 1] === newLines[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }

  // Backtrack
  const result: DiffLine[] = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      result.push({ type: 'same', text: oldLines[i - 1] });
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      result.push({ type: 'added', text: newLines[j - 1] });
      j--;
    } else {
      result.push({ type: 'removed', text: oldLines[i - 1] });
      i--;
    }
  }

  return result.reverse();
}

// ── Copy Button ────────────────────────────────────────────────────

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {
      // clipboard write failed — silently ignore
    });
  }, [text]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={copied ? 'Copied' : label}
      className="inline-flex items-center gap-1 text-text-tertiary hover:text-text-secondary transition-colors duration-150"
    >
      <CopyIcon />
      <span className="text-2xs">{copied ? 'Copied' : 'Copy'}</span>
    </button>
  );
}

// ── Main ContentBlock Dispatcher ───────────────────────────────────

export default function ContentBlock({ block }: BlockProps) {
  const type = block.type as string;

  if (type === 'text') {
    return (
      <div className="prose prose-invert prose-sm max-w-[65ch] [&_pre]:bg-bg-sunken [&_pre]:border [&_pre]:border-border [&_pre]:rounded-lg [&_code]:text-sm">
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

// ── ToolUseBlock — Collapsible Tool Call ───────────────────────────

function ToolUseBlock({ block }: BlockProps) {
  const [open, setOpen] = useState(false);
  const name = String(block.name || 'tool');
  const input = (block.input as Record<string, unknown>) ?? {};
  const { icon, summary } = describeToolUse(name, input);

  const toggle = useCallback(() => setOpen((o) => !o), []);

  return (
    <div className="rounded-lg border border-border bg-bg-sunken/60">
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
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-bg-sunken transition-colors duration-150 rounded-lg group"
      >
        <span className="text-text-tertiary shrink-0">
          <ChevronIcon open={open} />
        </span>
        <span className="text-text-tertiary shrink-0">{icon}</span>
        <span className="font-mono text-xs text-text-secondary whitespace-nowrap">{name}</span>
        <span className="text-sm text-text-tertiary truncate">{ellipsis(summary, 100)}</span>
        {name === 'Bash' && !!input.command && (
          <span className="ml-auto shrink-0 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
            <CopyButton text={String(input.command)} label="Copy command" />
          </span>
        )}
      </button>

      {open && <ToolUseDetail name={name} input={input} />}
    </div>
  );
}

const KNOWN_TOOLS_WITH_DETAIL = new Set([
  'Read', 'Write', 'Edit', 'MultiEdit', 'Bash', 'Grep', 'Glob',
  'WebFetch', 'WebSearch', 'TodoWrite', 'Task', 'TaskOutput', 'Agent',
]);

function ToolUseDetail({ name, input }: { name: string; input: Record<string, unknown> }) {
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

// ── Detail: Read / Write ───────────────────────────────────────────

function FileToolDetail({ input }: { input: Record<string, unknown> }) {
  const filePath = String(input.file_path ?? '');
  const content = input.content as string | undefined;
  const offset = input.offset as number | undefined;
  const limit = input.limit as number | undefined;

  return (
    <div className="border-t border-border px-3 py-2 space-y-1.5">
      {filePath && (
        <span className="text-mono-chip">{filePath}</span>
      )}
      {(offset !== undefined || limit !== undefined) && (
        <div className="text-xs text-text-tertiary">
          {offset !== undefined && <>Offset: {offset}</>}
          {offset !== undefined && limit !== undefined && ' · '}
          {limit !== undefined && <>Limit: {limit}</>}
        </div>
      )}
      {content !== undefined && (
        <pre className="text-xs font-mono bg-bg-sunken border border-border rounded-lg p-2.5 overflow-x-auto whitespace-pre-wrap max-h-80 overflow-y-auto">
          {content}
        </pre>
      )}
    </div>
  );
}

// ── Detail: Edit / MultiEdit (diff view) ───────────────────────────

function EditToolDetail({ name, input }: { name: string; input: Record<string, unknown> }) {
  const filePath = String(input.file_path ?? '');
  const oldString = String(input.old_string ?? '');
  const newString = String(input.new_string ?? '');
  const edits = input.edits as Array<Record<string, unknown>> | undefined;

  // MultiEdit: render each edit's diff
  if (name === 'MultiEdit' && edits && edits.length > 0) {
    return (
      <div className="border-t border-border px-3 py-2 space-y-3">
        {filePath && <span className="text-mono-chip">{filePath}</span>}
        {edits.map((ed, i) => {
          const oldS = String(ed.old_string ?? '');
          const newS = String(ed.new_string ?? '');
          return (
            <div key={i} className="space-y-1">
              <span className="text-2xs text-text-tertiary font-mono">Edit {i + 1} of {edits.length}</span>
              <DiffView oldStr={oldS} newStr={newS} />
            </div>
          );
        })}
      </div>
    );
  }

  // Single Edit
  return (
    <div className="border-t border-border px-3 py-2 space-y-2">
      {filePath && <span className="text-mono-chip">{filePath}</span>}
      <DiffView oldStr={oldString} newStr={newString} />
    </div>
  );
}

function DiffView({ oldStr, newStr }: { oldStr: string; newStr: string }) {
  const diff = computeLineDiff(oldStr, newStr);
  const hasRemoved = diff.some((d) => d.type === 'removed');
  const hasAdded = diff.some((d) => d.type === 'added');

  // If the diff is trivial (all same or all one side), show split view
  if (!hasRemoved && !hasAdded) {
    return (
      <pre className="text-xs font-mono bg-bg-sunken border border-border rounded-lg p-2.5 overflow-x-auto whitespace-pre-wrap text-text-secondary">
        {oldStr || '(empty)'}
      </pre>
    );
  }

  return (
    <div className="text-xs font-mono border border-border rounded-lg overflow-hidden">
      {diff.map((line, i) => {
        let lineClass = 'px-3 py-0.5';
        let prefix = '  ';
        if (line.type === 'removed') {
          lineClass += ' bg-[color-mix(in_srgb,var(--danger)_10%,transparent)] text-[var(--danger)]';
          prefix = '- ';
        } else if (line.type === 'added') {
          lineClass += ' bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] text-[var(--accent)]';
          prefix = '+ ';
        }
        return (
          <div key={i} className={lineClass}>
            <span className="select-none text-text-tertiary">{prefix}</span>
            {line.text || ' '}
          </div>
        );
      })}
    </div>
  );
}

// ── Detail: Bash (terminal well) ───────────────────────────────────

function BashToolDetail({ input }: { input: Record<string, unknown> }) {
  const command = String(input.command ?? '');
  const workdir = input.workdir as string | undefined;

  return (
    <div className="border-t border-border px-3 py-2 space-y-1.5">
      {workdir && (
        <div className="text-2xs text-text-tertiary">cd {workdir}</div>
      )}
      <div className="bg-bg-sunken border border-border rounded-lg overflow-hidden">
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-border">
          <span className="text-2xs font-mono text-text-tertiary">terminal</span>
          <CopyButton text={command} label="Copy command" />
        </div>
        <pre className="px-3 py-2 text-xs font-mono text-text-secondary overflow-x-auto whitespace-pre-wrap">
          <span className="select-none text-text-tertiary">{'$ '}</span>
          {command}
        </pre>
      </div>
    </div>
  );
}

// ── Detail: Grep / Glob ────────────────────────────────────────────

function SearchToolDetail({ input }: { input: Record<string, unknown> }) {
  const pattern = String(input.pattern ?? '');
  const path = input.path as string | undefined;
  const include = input.include as string | undefined;

  return (
    <div className="border-t border-border px-3 py-2 space-y-1.5">
      <div className="flex items-center gap-3 text-sm">
        <div>
          <span className="text-2xs text-text-tertiary">Pattern </span>
          <code className="text-mono-chip">{pattern}</code>
        </div>
        {path && (
          <div>
            <span className="text-2xs text-text-tertiary">Path </span>
            <code className="text-mono-chip">{path}</code>
          </div>
        )}
        {include && (
          <div>
            <span className="text-2xs text-text-tertiary">Include </span>
            <code className="text-mono-chip">{include}</code>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Detail: WebFetch / WebSearch ───────────────────────────────────

function WebToolDetail({ input }: { input: Record<string, unknown> }) {
  const url = String(input.url ?? '');
  const query = String(input.query ?? '');

  return (
    <div className="border-t border-border px-3 py-2">
      {url && (
        <div className="text-sm">
          <span className="text-2xs text-text-tertiary">URL </span>
          <code className="text-mono-chip break-all">{url}</code>
        </div>
      )}
      {query && (
        <div className="text-sm">
          <span className="text-2xs text-text-tertiary">Query </span>
          <span className="text-text-secondary">{query}</span>
        </div>
      )}
    </div>
  );
}

// ── Detail: TodoWrite ──────────────────────────────────────────────

function TodoToolDetail({ input }: { input: Record<string, unknown> }) {
  const todos = input.todos as Array<Record<string, unknown>> | undefined;

  if (!todos || todos.length === 0) {
    return (
      <div className="border-t border-border px-3 py-2">
        <span className="text-xs text-text-tertiary italic">No todo items</span>
      </div>
    );
  }

  const completed = todos.filter((t) => t.status === 'completed' || t.status === 'done').length;

  return (
    <div className="border-t border-border px-3 py-2 space-y-1">
      <span className="text-2xs text-text-tertiary">
        {completed}/{todos.length} completed
      </span>
      <ul className="space-y-0.5">
        {todos.map((todo, i) => {
          const done = todo.status === 'completed' || todo.status === 'done';
          const inProgress = todo.status === 'in_progress';
          return (
            <li key={i} className="flex items-start gap-2 text-sm">
              <span className={`shrink-0 mt-0.5 text-xs ${done ? 'text-[var(--accent)]' : inProgress ? 'text-[var(--warning)]' : 'text-text-tertiary'}`}>
                {done ? '◉' : inProgress ? '◌' : '○'}
              </span>
              <span className={done ? 'text-text-tertiary line-through' : 'text-text-secondary'}>
                {String(todo.content ?? todo.text ?? '')}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ── Detail: Task / Agent / TaskOutput ──────────────────────────────

function AgentToolDetail({ input }: { input: Record<string, unknown> }) {
  const description = String(input.description ?? '');
  const subagentType = input.subagent_type as string | undefined;
  const prompt = input.prompt as string | undefined;
  const taskId = input.task_id as string | undefined;

  return (
    <div className="border-t border-border px-3 py-2 space-y-1.5">
      {subagentType && (
        <div className="text-xs">
          <span className="text-2xs text-text-tertiary">Agent type </span>
          <span className="text-mono-chip">{subagentType}</span>
        </div>
      )}
      {taskId && (
        <div className="text-xs">
          <span className="text-2xs text-text-tertiary">Task ID </span>
          <span className="text-mono-chip">{taskId}</span>
        </div>
      )}
      {(description || prompt) && (
        <p className="text-sm text-text-secondary">{description || prompt}</p>
      )}
    </div>
  );
}

// ── Detail: Unknown Tool ───────────────────────────────────────────

function UnknownToolDetail({ input }: { input: Record<string, unknown> }) {
  const [showRaw, setShowRaw] = useState(false);
  const keys = Object.keys(input);

  return (
    <div className="border-t border-border px-3 py-2 space-y-2">
      {keys.length > 0 && (
        <div className="space-y-1">
          {keys.map((k) => {
            const val = input[k];
            let display: string;
            if (typeof val === 'string') {
              display = ellipsis(val, 80);
            } else if (typeof val === 'object' && val !== null) {
              display = JSON.stringify(val).slice(0, 80);
            } else {
              display = String(val);
            }
            return (
              <div key={k} className="flex items-start gap-2 text-sm">
                <span className="text-2xs font-mono text-text-tertiary shrink-0">{k}</span>
                <span className="text-xs font-mono text-text-secondary truncate">{display}</span>
              </div>
            );
          })}
        </div>
      )}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setShowRaw((v) => !v); }}
        className="text-xs text-text-tertiary hover:text-text-secondary transition-colors duration-150"
      >
        {showRaw ? 'Hide raw' : 'Show raw'}
      </button>
      {showRaw && (
        <pre className="text-xs font-mono bg-bg-sunken border border-border rounded-lg p-2.5 overflow-x-auto whitespace-pre-wrap max-h-80 overflow-y-auto">
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ── ToolResultBlock — Collapsible Result ───────────────────────────

const ERROR_PATTERNS = [/^Error/i, /^Traceback/i, /Traceback \(most recent call last\)/];

function looksLikeError(content: string): boolean {
  return ERROR_PATTERNS.some((re) => re.test(content));
}

function ToolResultBlock({ block }: BlockProps) {
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
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-bg-sunken transition-colors duration-150 rounded-lg"
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
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
                className="text-xs text-text-tertiary hover:text-text-secondary transition-colors duration-150"
              >
                {expanded ? 'Show less' : `Show full output (${lineCount} lines)`}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── ThinkingBlock — Internal Reasoning (preserved; Chunk B refines) ─

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg bg-bg-sunken border border-border">
      <button
        onClick={() => setOpen(!open)}
        className="w-full px-3 py-1.5 text-left text-sm text-text-tertiary hover:text-text-secondary transition-colors flex items-center gap-2"
      >
        <span>Thinking</span>
        <span>{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <pre className="px-3 py-2 border-t border-border text-sm text-text-tertiary whitespace-pre-wrap max-h-80 overflow-y-auto">
          {text}
        </pre>
      )}
    </div>
  );
}
