import { useParams, useNavigate, Link } from 'react-router-dom';
import { useHandoff, useHandoffSummary } from '../hooks/useHandoffs';
import { useAuth } from '../auth/AuthContext';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { formatTokens } from '../utils/tokens';
import { abbreviateModel } from '../utils/models';
import CopyButton from '../components/CopyButton';
import RelativeDate from '../components/RelativeDate';
import type { HandoffDetail as HandoffDetailType, HandoffSessionSummary } from '../api/client';

/* ------------------------------------------------------------------ */
/*  Status Stepper                                                     */
/* ------------------------------------------------------------------ */

const STEPS = ['Created', 'Pending', 'Claimed', 'Resumed'] as const;

function getStepState(
  stepIndex: number,
  status: HandoffDetailType['status'],
): 'completed' | 'current' | 'future' | 'expired' {
  // Map status to the "current" step index
  const currentIndex =
    status === 'pending' ? 1 : status === 'claimed' ? 2 : status === 'expired' ? 2 : 1;

  if (status === 'expired' && stepIndex === 2) return 'expired';
  if (stepIndex < currentIndex) return 'completed';
  if (stepIndex === currentIndex) return 'current';
  return 'future';
}

function getStepTimestamp(
  stepIndex: number,
  handoff: HandoffDetailType,
): string | null {
  if (stepIndex === 0) return handoff.created_at;
  if (stepIndex === 1) return handoff.created_at; // pending starts at creation
  if (stepIndex === 2 && handoff.claimed_at) return handoff.claimed_at;
  return null;
}

function HandoffStepper({ handoff }: { handoff: HandoffDetailType }) {
  const isExpired = handoff.status === 'expired';
  const steps = isExpired
    ? ['Created', 'Pending', 'Expired', 'Resumed']
    : STEPS;

  return (
    <div className="border border-border rounded-lg bg-bg-secondary p-4 mb-4">
      <div className="flex items-center">
        {steps.map((label, i) => {
          const state = getStepState(i, handoff.status);
          const timestamp = getStepTimestamp(i, handoff);

          return (
            <div key={label} className="flex items-center flex-1 last:flex-none">
              {/* Step circle + label */}
              <div className="flex flex-col items-center">
                <div
                  className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${
                    state === 'completed'
                      ? 'bg-green-500 text-white'
                      : state === 'current'
                        ? 'bg-accent text-white animate-pulse'
                        : state === 'expired'
                          ? 'bg-red-500 text-white'
                          : 'bg-border text-text-muted'
                  }`}
                >
                  {state === 'completed' ? (
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                  ) : state === 'expired' ? (
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  ) : (
                    <span>{i + 1}</span>
                  )}
                </div>
                <span
                  className={`mt-1.5 text-sm whitespace-nowrap ${
                    state === 'completed'
                      ? 'text-text-primary font-semibold'
                      : state === 'current'
                        ? 'text-accent font-semibold'
                        : state === 'expired'
                          ? 'text-red-400 font-semibold'
                          : 'text-text-muted'
                  }`}
                >
                  {label}
                </span>
                {timestamp && (state === 'completed' || state === 'current') && (
                  <span className="text-[11px] text-text-muted mt-0.5">
                    <RelativeDate iso={timestamp} />
                  </span>
                )}
              </div>

              {/* Connector line (not after last step) */}
              {i < steps.length - 1 && (
                <div
                  className={`h-0.5 flex-1 mx-2 ${
                    getStepState(i + 1, handoff.status) === 'future'
                      ? 'bg-border'
                      : getStepState(i + 1, handoff.status) === 'expired'
                        ? 'bg-red-500/40'
                        : 'bg-green-500'
                  }`}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Session Context Card                                               */
/* ------------------------------------------------------------------ */

function SessionContextCard({ summary }: { summary: HandoffSessionSummary }) {
  const hasTests = summary.tests_run > 0;
  const filesCapped = summary.files_modified.slice(0, 5);
  const extraFiles = summary.files_modified.length - 5;
  const lastMessage = summary.last_assistant_messages[0];

  return (
    <div className="border border-border rounded-lg bg-bg-secondary p-4 mb-4">
      <h3 className="text-sm font-semibold text-text-primary mb-3">Session Context</h3>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm mb-3">
        <div>
          <span className="text-text-muted block">Tool</span>
          <span className="text-text-secondary">{summary.tool}</span>
        </div>
        <div>
          <span className="text-text-muted block">Model</span>
          <span className="text-text-secondary">{summary.model ? abbreviateModel(summary.model) : '-'}</span>
        </div>
        <div>
          <span className="text-text-muted block">Messages</span>
          <span className="text-text-secondary">{summary.message_count}</span>
        </div>
        <div>
          <span className="text-text-muted block">Commands</span>
          <span className="text-text-secondary">{summary.commands_executed}</span>
        </div>
      </div>

      {/* Files modified */}
      {summary.files_modified.length > 0 && (
        <div className="mb-3">
          <span className="text-text-muted text-sm block mb-1">Files modified</span>
          <div className="flex flex-wrap gap-1">
            {filesCapped.map((f) => (
              <span
                key={f}
                className="text-sm bg-bg-primary text-text-secondary px-1.5 py-0.5 rounded border border-border truncate max-w-[200px]"
                title={f}
              >
                {f}
              </span>
            ))}
            {extraFiles > 0 && (
              <span className="text-sm text-text-muted px-1.5 py-0.5">
                +{extraFiles} more
              </span>
            )}
          </div>
        </div>
      )}

      {/* Test results */}
      {hasTests && (
        <div className="mb-3">
          <span className="text-text-muted text-sm block mb-1">Tests</span>
          <div className="flex gap-2">
            <span className="text-sm px-1.5 py-0.5 rounded bg-green-500/10 text-green-400 border border-green-500/30">
              {summary.tests_passed} passed
            </span>
            {summary.tests_failed > 0 && (
              <span className="text-sm px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/30">
                {summary.tests_failed} failed
              </span>
            )}
          </div>
        </div>
      )}

      {/* Errors */}
      {summary.errors_encountered.length > 0 && (
        <div className="mb-3">
          <span className="text-text-muted text-sm block mb-1">Errors</span>
          <div className="space-y-1">
            {summary.errors_encountered.map((err, i) => (
              <p key={i} className="text-sm text-red-400 bg-red-500/5 px-2 py-1 rounded border border-red-500/20 truncate">
                {err}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* Last activity */}
      {lastMessage && (
        <div>
          <span className="text-text-muted text-sm block mb-1">Last activity</span>
          <p className="text-sm text-text-secondary bg-bg-primary px-2 py-1.5 rounded border border-border line-clamp-3">
            {lastMessage}
          </p>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Status Badge                                                       */
/* ------------------------------------------------------------------ */

function StatusBadge({ status }: { status: HandoffDetailType['status'] }) {
  const styles = {
    pending: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/30',
    claimed: 'bg-green-500/10 text-green-400 border-green-500/30',
    expired: 'bg-neutral-500/10 text-neutral-400 border-neutral-500/30',
  };
  return (
    <span className={`px-2 py-1 text-sm border rounded ${styles[status]}`}>
      {status}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

export default function HandoffDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const { data: handoff, isLoading, error } = useHandoff(id!);
  const { data: summary } = useHandoffSummary(id!);

  const claimMutation = useMutation({
    mutationFn: () => auth!.client.claimHandoff(id!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['handoff', id] });
      void queryClient.invalidateQueries({ queryKey: ['handoffs'] });
    },
  });

  if (isLoading) {
    return <div className="p-8 text-text-muted">Loading handoff...</div>;
  }

  if (error || !handoff) {
    return (
      <div className="p-8">
        <button onClick={() => navigate('/handoffs')} className="text-accent text-sm mb-4 hover:underline">
          &larr; Back to Handoffs
        </button>
        <p className="text-red-400">Failed to load handoff: {String(error)}</p>
      </div>
    );
  }

  const isRecipient = handoff.status === 'pending';
  const pullCommand = `sfs pull ${handoff.session_id}`;

  return (
    <div className="max-w-3xl mx-auto px-4 py-6">
      <button
        onClick={() => navigate('/handoffs')}
        className="text-accent text-sm mb-4 hover:underline"
      >
        &larr; Back to Handoffs
      </button>

      {/* Status stepper */}
      <HandoffStepper handoff={handoff} />

      {/* Session context from summary */}
      {summary && <SessionContextCard summary={summary} />}

      {/* Session preview card */}
      <div className="border border-border rounded-lg bg-bg-secondary p-4 mb-4">
        <div className="flex items-start justify-between mb-3">
          <h2 className="text-base font-medium text-text-primary break-words">
            {handoff.session_title || 'Untitled session'}
          </h2>
          <StatusBadge status={handoff.status} />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
          <div>
            <span className="text-text-muted block">Tool</span>
            <span className="text-text-secondary">{handoff.session_tool}</span>
          </div>
          <div>
            <span className="text-text-muted block">Model</span>
            <span className="text-text-secondary">{abbreviateModel(handoff.session_model_id)}</span>
          </div>
          <div>
            <span className="text-text-muted block">Messages</span>
            <span className="text-text-secondary">{handoff.session_message_count}</span>
          </div>
          <div>
            <span className="text-text-muted block">Tokens</span>
            <span className="text-text-secondary">{formatTokens(handoff.session_total_tokens ?? 0)}</span>
          </div>
        </div>
      </div>

      {/* Sender message */}
      {handoff.message && (
        <div className="border-l-2 border-accent/50 pl-3 mb-4">
          <p className="text-sm text-text-muted mb-1">Message from {handoff.sender_email}</p>
          <p className="text-sm text-text-secondary whitespace-pre-wrap">{handoff.message}</p>
        </div>
      )}

      {/* Claim action */}
      {isRecipient && (
        <div className="mb-4">
          <button
            onClick={() => claimMutation.mutate()}
            disabled={claimMutation.isPending}
            className="px-4 py-2 bg-accent text-white text-sm rounded hover:bg-accent/90 transition-colors disabled:opacity-50"
          >
            {claimMutation.isPending ? 'Claiming...' : 'Claim this handoff'}
          </button>
          {claimMutation.isError && (
            <p className="text-red-400 text-sm mt-2">
              Failed to claim: {String(claimMutation.error)}
            </p>
          )}
        </div>
      )}

      {/* CLI pull command */}
      <div className="border border-border rounded-lg bg-bg-secondary p-3 mb-4">
        <p className="text-sm text-text-muted mb-2">Pull via CLI</p>
        <div className="flex items-center gap-2">
          <code className="text-sm text-text-secondary bg-bg-primary px-2 py-1 rounded flex-1 truncate">
            {pullCommand}
          </code>
          <CopyButton text={pullCommand} label="Copy" />
        </div>
      </div>

      {/* Timestamps */}
      <div className="text-sm text-text-muted space-y-1 mb-4">
        <div className="flex gap-2">
          <span>Sent:</span>
          <RelativeDate iso={handoff.created_at} />
        </div>
        {handoff.claimed_at && (
          <div className="flex gap-2">
            <span>Claimed:</span>
            <RelativeDate iso={handoff.claimed_at} />
          </div>
        )}
      </div>

      {/* Participants */}
      <div className="text-sm text-text-muted space-y-1 mb-4">
        <div>From: <span className="text-text-secondary">{handoff.sender_email}</span></div>
        <div>To: <span className="text-text-secondary">{handoff.recipient_email}</span></div>
      </div>

      {/* Link to full session if claimed */}
      {handoff.status === 'claimed' && (
        <Link
          to={`/sessions/${handoff.session_id}`}
          className="text-accent text-sm hover:underline"
        >
          View full session &rarr;
        </Link>
      )}
    </div>
  );
}
