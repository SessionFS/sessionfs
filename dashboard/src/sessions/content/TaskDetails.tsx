import { useState } from 'react';
import { Button } from '../../components/ui';
import { ellipsis } from './text';

export function TodoToolDetail({ input }: { input: Record<string, unknown> }) {
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

export function AgentToolDetail({ input }: { input: Record<string, unknown> }) {
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

export function UnknownToolDetail({ input }: { input: Record<string, unknown> }) {
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
      <Button
        variant="ghost"
        size="sm"
        onClick={(e) => { e.stopPropagation(); setShowRaw((v) => !v); }}
        className="!px-1.5 !py-0.5 font-normal text-text-tertiary"
      >
        {showRaw ? 'Hide raw' : 'Show raw'}
      </Button>
      {showRaw && (
        <pre className="text-xs font-mono bg-bg-sunken border border-border rounded-lg p-2.5 overflow-x-auto whitespace-pre-wrap max-h-80 overflow-y-auto">
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
    </div>
  );
}
