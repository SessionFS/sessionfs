import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useJudgeSettings } from '../hooks/useJudgeSettings';
import { Button } from '../components/ui/Button';
import Skeleton from '../components/Skeleton';
import { Alert } from '../components/ui/Alert';
import { Card } from '../components/ui/Card';
import { Icon } from '../components/ui/Icon';
import { FileText } from 'lucide-react';

interface Props {
  sessionId: string;
}

export default function SummaryTab({ sessionId }: Props) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const { data: judgeSettings } = useJudgeSettings();
  const [showAllRead, setShowAllRead] = useState(false);
  const [showErrors, setShowErrors] = useState(false);

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

  const generateNarrative = useMutation({
    mutationFn: () => auth!.client.generateNarrativeSummary(sessionId, {
      model: judgeSettings?.model || undefined,
      provider: judgeSettings?.provider || undefined,
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['summary', sessionId] }),
  });

  const is404 = error && 'status' in error && (error as { status: number }).status === 404;

  if (!isLoading && (is404 || (!summary && !error))) {
    return (
      <div className="flex flex-col items-center justify-center py-16 px-4 text-center">
        <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-surface-active text-text-tertiary">
          <Icon icon={FileText} size={20} />
        </div>
        <p className="text-md font-semibold text-text-primary mb-1">No summary yet</p>
        <p className="text-sm text-text-tertiary mb-4">
          Generate a summary to see files changed, activity, and metrics.
        </p>
        <Button onClick={() => generate.mutate()} disabled={generate.isPending} loading={generate.isPending}>
          {generate.isPending ? 'Generating…' : 'Generate Summary'}
        </Button>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="py-16 px-4">
        <Skeleton lines={5} type="text" />
      </div>
    );
  }

  if (error && !is404) {
    return <div className="p-4 text-danger text-sm">Failed to load summary.</div>;
  }

  if (!summary) return null;

  const duration = summary.duration_minutes < 60
    ? `${summary.duration_minutes}m`
    : `${(summary.duration_minutes / 60).toFixed(1)}h`;

  return (
    <div className="p-4 max-w-3xl mx-auto">
      {/* Metric cards */}
      <div className="grid grid-cols-4 gap-3 mb-6">
        <MetricCard label="Duration" value={duration} />
        <MetricCard label="Messages" value={String(summary.message_count)} />
        <MetricCard label="Tool calls" value={String(summary.tool_call_count)} />
        <MetricCard label="Tests"
          value={summary.tests_run > 0 ? `${summary.tests_passed}/${summary.tests_run}` : '-'}
          color={summary.tests_failed > 0 ? 'text-warning' : summary.tests_run > 0 ? 'text-success' : undefined}
        />
      </div>

      {summary.branch && (
        <p className="text-sm text-text-muted mb-6">
          Branch: <span className="text-text-secondary">{summary.branch}</span>
          {summary.commit && <span className="text-text-tertiary"> @ {summary.commit}</span>}
        </p>
      )}

      {/* Files */}
      {summary.files_modified.length > 0 && (
        <section className="mb-4">
          <h2 className="text-md! leading-[1.4]! tracking-normal! font-semibold text-text-primary mb-1">
            Files modified ({summary.files_modified.length})
          </h2>
          <div className="space-y-0.5">
            {summary.files_modified.map((f) => (
              <p key={f} className="font-mono text-sm text-text-tertiary">{f}</p>
            ))}
          </div>
        </section>
      )}

      {summary.files_read.length > 0 && (
        <section className="mb-4">
          <h2 className="text-md! leading-[1.4]! tracking-normal! font-semibold text-text-primary mb-1">
            Files read ({summary.files_read.length})
          </h2>
          <div className="space-y-0.5">
            {(showAllRead ? summary.files_read : summary.files_read.slice(0, 5)).map((f) => (
              <p key={f} className="font-mono text-sm text-text-tertiary">{f}</p>
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
        <h2 className="text-md! leading-[1.4]! tracking-normal! font-semibold text-text-primary mb-1">Activity</h2>
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
                className="text-danger hover:underline">
                {summary.errors_encountered.length} errors encountered
              </button>
              {showErrors && (
                <div className="mt-1 space-y-1">
                  {summary.errors_encountered.map((e, i) => (
                    <pre key={i} className="text-xs text-danger/80 bg-danger/10 rounded px-2 py-1 overflow-x-auto">
                      {e}
                    </pre>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </section>

      {/* Narrative Summary */}
      {summary.what_happened ? (
        <Card className="mb-4 p-4">
          <h2 className="text-md! leading-[1.4]! tracking-normal! font-semibold text-text-primary mb-3">Narrative Summary</h2>

          <div className="mb-3">
            <h3 className="text-micro text-text-tertiary uppercase mb-1">What happened</h3>
            <p className="text-sm text-text-primary">{summary.what_happened}</p>
          </div>

          {summary.key_decisions && summary.key_decisions.length > 0 && (
            <div className="mb-3">
              <h3 className="text-micro text-text-tertiary uppercase mb-1">Key decisions</h3>
              <ul className="text-sm text-text-primary space-y-0.5 list-disc list-inside">
                {summary.key_decisions.map((d, i) => <li key={i}>{d}</li>)}
              </ul>
            </div>
          )}

          {summary.outcome && (
            <div className="mb-3">
              <h3 className="text-micro text-text-tertiary uppercase mb-1">Outcome</h3>
              <p className="text-sm text-text-primary">{summary.outcome}</p>
            </div>
          )}

          {summary.open_issues && summary.open_issues.length > 0 && (
            <div className="mb-3">
              <h3 className="text-micro text-text-tertiary uppercase mb-1">Open issues</h3>
              <ul className="text-sm text-text-primary space-y-0.5 list-disc list-inside">
                {summary.open_issues.map((d, i) => <li key={i}>{d}</li>)}
              </ul>
            </div>
          )}

          {summary.narrative_model && (
            <p className="text-2xs text-text-tertiary mt-2 opacity-50">Generated by {summary.narrative_model}</p>
          )}
        </Card>
      ) : (
        <Card className="mb-4 flex flex-col items-center py-6">
          {summary.narrative_error ? (
            <Alert variant="danger" title="Could not generate narrative" className="w-full mb-2">
              {summary.narrative_error.message}
            </Alert>
          ) : (
            <p className="text-sm text-text-secondary mb-2">No narrative summary yet.</p>
          )}
          {!judgeSettings?.key_set ? (
            <p className="text-xs text-text-tertiary">Configure judge settings to enable narrative generation.</p>
          ) : (
            <>
              <Button
                onClick={() => generateNarrative.mutate()}
                disabled={generateNarrative.isPending}
                loading={generateNarrative.isPending}
              >
                {generateNarrative.isPending ? 'Generating…' : 'Generate Narrative'}
              </Button>
              {generateNarrative.isError && (
                <p className="text-xs text-danger mt-2">
                  Failed to generate narrative. Check your judge settings.
                </p>
              )}
            </>
          )}
        </Card>
      )}

      {summary.generated_at && (
        <p className="text-xs text-text-tertiary mt-6">
          Generated {new Date(summary.generated_at).toLocaleString()}
        </p>
      )}
    </div>
  );
}

function MetricCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <Card className="p-3 text-center">
      <div className={`text-2xl font-bold tabular-nums ${color || 'text-text-secondary'}`}>{value}</div>
      <div className="text-micro text-text-tertiary uppercase mt-0.5">{label}</div>
    </Card>
  );
}
