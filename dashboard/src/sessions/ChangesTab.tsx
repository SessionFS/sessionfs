import { useChanges } from '../hooks/useChanges';
import { Badge, Card } from '../components/ui';
import Skeleton from '../components/Skeleton';
import type { FileChange } from '../api/client';

interface Props {
  sessionId: string;
}

const OPERATION_BADGE: Record<
  FileChange['operation'],
  { variant: 'success' | 'info' | 'danger' | 'default'; label: string }
> = {
  created: { variant: 'success', label: 'created' },
  edited: { variant: 'info', label: 'edited' },
  deleted: { variant: 'danger', label: 'deleted' },
  unknown: { variant: 'default', label: 'unknown' },
};

export default function ChangesTab({ sessionId }: Props) {
  const { data, isLoading, error } = useChanges(sessionId);

  if (isLoading) {
    return (
      <div className="p-4">
        <Skeleton lines={5} type="text" />
      </div>
    );
  }

  if (error) {
    return <div className="p-4 text-danger text-sm">Failed to load changes.</div>;
  }

  const files = data?.files ?? [];

  if (files.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <svg className="w-10 h-10 text-text-tertiary mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="16" y1="13" x2="8" y2="13" />
          <line x1="16" y1="17" x2="8" y2="17" />
        </svg>
        <p className="text-md font-semibold text-text-primary mb-1">No file changes recorded</p>
        <p className="text-sm text-text-tertiary">This tool doesn't record file changes.</p>
      </div>
    );
  }

  return (
    <div className="p-4 max-w-3xl mx-auto">
      {data?.git_ref && (
        <p className="text-sm text-text-muted mb-3">
          <span className="text-text-tertiary">Ref:</span>{' '}
          <span className="font-mono text-text-secondary">{data.git_ref}</span>
        </p>
      )}
      {data?.truncated && (
        <p className="text-xs text-warning mb-3">Large session — showing a truncated set of changes.</p>
      )}
      <div className="space-y-2">
        {files.map((file) => (
          <FileChangeCard key={file.path} file={file} />
        ))}
      </div>
    </div>
  );
}

function FileChangeCard({ file }: { file: FileChange }) {
  const badge = OPERATION_BADGE[file.operation] ?? OPERATION_BADGE.unknown;
  const changeCount = file.added_lines > 0 || file.removed_lines > 0;

  return (
    <Card className="overflow-hidden">
      <details>
        <summary className="cursor-pointer select-none flex items-center gap-2 px-3 py-2.5 hover:bg-bg-sunken">
          <Badge variant={badge.variant} label={badge.label} size="sm" />
          <span className="font-mono text-sm text-text-primary truncate flex-1">{file.path}</span>
          {changeCount && (
            <span className="text-xs font-mono tabular-nums shrink-0">
              <span className="text-[var(--accent)]">+{file.added_lines}</span>{' '}
              <span className="text-[var(--danger)]">-{file.removed_lines}</span>
            </span>
          )}
          {file.truncated && <span className="text-2xs text-text-tertiary shrink-0">truncated</span>}
        </summary>
        <div className="border-t border-border">
          {file.diff ? (
            <DiffText diff={file.diff} />
          ) : (
            <p className="px-3 py-2 text-xs text-text-tertiary italic">No diff available.</p>
          )}
        </div>
      </details>
    </Card>
  );
}

function DiffText({ diff }: { diff: string }) {
  const lines = diff.split('\n');
  return (
    <pre className="text-xs font-mono bg-bg-sunken overflow-x-auto max-h-96 overflow-y-auto py-1">
      {lines.map((line, i) => {
        let cls = 'px-3 py-0.5';
        if (line.startsWith('+') && !line.startsWith('+++')) {
          cls += ' bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] text-[var(--accent)]';
        } else if (line.startsWith('-') && !line.startsWith('---')) {
          cls += ' bg-[color-mix(in_srgb,var(--danger)_10%,transparent)] text-[var(--danger)]';
        } else if (line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++') || line.startsWith('diff --git')) {
          cls += ' text-text-tertiary';
        } else {
          cls += ' text-text-secondary';
        }
        return (
          <div key={i} className={cls}>
            {line || ' '}
          </div>
        );
      })}
    </pre>
  );
}
