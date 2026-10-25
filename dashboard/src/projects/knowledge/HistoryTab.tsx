import { useCompilations } from '../../hooks/useProjects';
import RelativeDate from '../../components/RelativeDate';

export default function HistoryTab({ projectId }: { projectId: string }) {
  const { data, isLoading } = useCompilations(projectId);

  if (isLoading) {
    return <p className="p-5 text-text-tertiary text-sm">Loading history…</p>;
  }

  const compilations = data?.compilations || [];

  if (!compilations.length) {
    return (
      <p className="p-5 text-text-tertiary text-sm text-center py-8">
        No compilations yet. Compile pending entries to create the first compilation.
      </p>
    );
  }

  return (
    <div className="p-5 space-y-3">
      {compilations.map((c) => (
        <div
          key={c.id}
          className="px-4 py-3 rounded-lg border border-border bg-bg-elevated"
        >
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-text-primary">
              {c.entries_compiled} {c.entries_compiled === 1 ? 'entry' : 'entries'} compiled
            </span>
            <span className="text-xs text-text-tertiary">
              {c.trigger === 'scheduled' ? 'scheduled' : 'manual'} &middot;{' '}
              <RelativeDate iso={c.compiled_at} />
            </span>
          </div>
          {(c.context_words_before > 0 || c.context_words_after > 0) && (
            <p className="text-xs text-text-tertiary mt-1">
              Context: {c.context_words_before} words &rarr; {c.context_words_after} words
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
