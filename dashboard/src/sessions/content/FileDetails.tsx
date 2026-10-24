import { computeLineDiff } from './text';

export function FileToolDetail({ input }: { input: Record<string, unknown> }) {
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

export function EditToolDetail({ name, input }: { name: string; input: Record<string, unknown> }) {
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

export function DiffView({ oldStr, newStr }: { oldStr: string; newStr: string }) {
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
