import { CopyButton } from './CopyButton';

export function BashToolDetail({ input }: { input: Record<string, unknown> }) {
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

export function SearchToolDetail({ input }: { input: Record<string, unknown> }) {
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

export function WebToolDetail({ input }: { input: Record<string, unknown> }) {
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
