import { KIND_TONE } from './constants';

export function KindBadge({ kind }: { kind: string }) {
  const label = kind === 'issue' ? 'Issue' : 'Task';
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wide ${
        KIND_TONE[kind] ?? KIND_TONE.task
      }`}
    >
      {label}
    </span>
  );
}

/* ── List view ── */
