import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

interface Props {
  sessionId: string;
}

export default function SummaryTab({ sessionId }: Props) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  const { data: summary, isLoading, error } = useQuery({
    queryKey: ['summary', sessionId],
    queryFn: () => auth!.client.getSessionSummary(sessionId),
    enabled: !!auth,
    staleTime: 300_000,
    retry: (count, err) => {
      if (err && 'status' in err && (err as { status: number }).status === 404) return false;
      return count < 1;
    },
  });

  const generate = useMutation({
    mutationFn: () => auth!.client.generateSessionSummary(sessionId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['summary', sessionId] }),
  });

  const is404 = error && 'status' in error && (error as { status: number }).status === 404;

  if (!isLoading && (is404 || (!summary && !error))) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <p className="text-text-secondary mb-2">No summary generated yet.</p>
        <button onClick={() => generate.mutate()} disabled={generate.isPending}
          className="px-4 py-2 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors disabled:opacity-50">
          {generate.isPending ? 'Generating...' : 'Generate Summary'}
        </button>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin mb-3" />
        <p className="text-text-muted text-sm">Loading summary...</p>
      </div>
    );
  }

  if (error && !is404) {
    return <div className="p-4 text-red-400 text-sm">Failed to load summary.</div>;
  }

  if (!summary) return null;

  const duration = summary.duration_minutes < 60
    ? `${summary.duration_minutes}m`
    : `${(summary.duration_minutes / 60).toFixed(1)}h`;

  const [showAllRead, setShowAllRead] = useState(false);
  const [showErrors, setShowErrors] = useState(false);

  return (
    <div className="p-4 max-w-3xl mx-auto">
      {/* Metric cards */}
      <div className="grid grid-cols-4 gap-3 mb-6">
        <Card label="Duration" value={duration} />
        <Card label="Messages" value={String(summary.message_count)} />
        <Card label="Tool calls" value={String(summary.tool_call_count)} />
        <Card label="Tests"
          value={summary.tests_run > 0 ? `${summary.tests_passed}/${summary.tests_run}` : '-'}
          color={summary.tests_failed > 0 ? 'text-yellow-400' : summary.tests_run > 0 ? 'text-green-400' : undefined}
        />
      </div>

      {summary.branch && (
        <p className="text-sm text-text-muted mb-6">
          Branch: <span className="text-text-secondary">{summary.branch}</span>
          {summary.commit && <span className="text-text-muted/50"> @ {summary.commit}</span>}
        </p>
      )}

      {/* Files */}
      {summary.files_modified.length > 0 && (
        <section className="mb-4">
          <h3 className="text-sm font-medium text-text-secondary mb-1">
            Files modified ({summary.files_modified.length})
          </h3>
          <div className="space-y-0.5">
            {summary.files_modified.map((f) => (
              <p key={f} className="text-sm text-text-muted font-mono">{f}</p>
            ))}
          </div>
        </section>
      )}

      {summary.files_read.length > 0 && (
        <section className="mb-4">
          <h3 className="text-sm font-medium text-text-secondary mb-1">
            Files read ({summary.files_read.length})
          </h3>
          <div className="space-y-0.5">
            {(showAllRead ? summary.files_read : summary.files_read.slice(0, 5)).map((f) => (
              <p key={f} className="text-sm text-text-muted font-mono">{f}</p>
            ))}
            {summary.files_read.length > 5 && !showAllRead && (
              <button onClick={() => setShowAllRead(true)}
                className="text-xs text-accent hover:underline">
                + {summary.files_read.length - 5} more
              </button>
            )}
          </div>
        </section>
      )}

      {/* Activity */}
      <section className="mb-4">
        <h3 className="text-sm font-medium text-text-secondary mb-1">Activity</h3>
        <div className="space-y-0.5 text-sm text-text-muted">
          <p>Commands: {summary.commands_executed}</p>
          {summary.tests_run > 0 && (
            <p>Tests: {summary.tests_run} runs ({summary.tests_passed} passed, {summary.tests_failed} failed)</p>
          )}
          {summary.packages_installed.length > 0 && (
            <p>Packages: {summary.packages_installed.join(', ')}</p>
          )}
          {summary.errors_encountered.length > 0 && (
            <>
              <button onClick={() => setShowErrors(!showErrors)}
                className="text-red-400 hover:underline">
                {summary.errors_encountered.length} errors encountered
              </button>
              {showErrors && (
                <div className="mt-1 space-y-1">
                  {summary.errors_encountered.map((e, i) => (
                    <pre key={i} className="text-xs text-red-400/80 bg-red-500/10 rounded px-2 py-1 overflow-x-auto">
                      {e}
                    </pre>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </section>

      {/* Narrative (if generated) */}
      {summary.what_happened && (
        <section className="mb-4 p-3 bg-bg-secondary border border-border rounded-lg">
          <h3 className="text-sm font-medium text-text-secondary mb-1">What happened</h3>
          <p className="text-sm text-text-primary">{summary.what_happened}</p>
        </section>
      )}
      {summary.key_decisions && summary.key_decisions.length > 0 && (
        <section className="mb-4">
          <h3 className="text-sm font-medium text-text-secondary mb-1">Key decisions</h3>
          <ul className="text-sm text-text-muted space-y-0.5">
            {summary.key_decisions.map((d, i) => <li key={i}>- {d}</li>)}
          </ul>
        </section>
      )}
      {summary.outcome && (
        <section className="mb-4">
          <h3 className="text-sm font-medium text-text-secondary mb-1">Outcome</h3>
          <p className="text-sm text-text-primary">{summary.outcome}</p>
        </section>
      )}

      {summary.generated_at && (
        <p className="text-xs text-text-muted/50 mt-6">
          Generated {new Date(summary.generated_at).toLocaleString()}
        </p>
      )}
    </div>
  );
}

function Card({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-bg-secondary border border-border rounded-lg p-3 text-center">
      <div className={`text-xl font-semibold tabular-nums ${color || 'text-text-secondary'}`}>{value}</div>
      <div className="text-[10px] text-text-muted uppercase tracking-wider mt-0.5">{label}</div>
    </div>
  );
}
