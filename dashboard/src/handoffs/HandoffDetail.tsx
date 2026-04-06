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
    <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 mb-5">
      <div className="flex items-center">
        {steps.map((label, i) => {
          const state = getStepState(i, handoff.status);
          const timestamp = getStepTimestamp(i, handoff);

          return (
            <div key={label} className="flex items-center flex-1 last:flex-none">
              {/* Step circle + label */}
              <div className="flex flex-col items-center">
                <div
                  className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-medium transition-colors ${
                    state === 'completed'
                      ? 'bg-[var(--brand)] text-white'
                      : state === 'current'
                        ? 'bg-[var(--brand)] text-white shadow-[0_0_0_4px_var(--bg-elevated),0_0_0_6px_var(--brand)]'
                        : state === 'expired'
                          ? 'bg-red-500 text-white'
                          : 'border-2 border-[var(--border)] text-[var(--text-tertiary)] bg-[var(--bg-elevated)]'
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
                  className={`mt-2 text-[13px] font-medium whitespace-nowrap ${
                    state === 'completed'
                      ? 'text-[var(--text-primary)]'
                      : state === 'current'
                        ? 'text-[var(--brand)]'
                        : state === 'expired'
                          ? 'text-red-500'
                          : 'text-[var(--text-tertiary)]'
                  }`}
                >
                  {label}
                </span>
                {timestamp && (state === 'completed' || state === 'current') && (
                  <span className="text-[11px] text-[var(--text-tertiary)] mt-0.5">
                    <RelativeDate iso={timestamp} />
                  </span>
                )}
              </div>

              {/* Connector line (not after last step) */}
              {i < steps.length - 1 && (
                <div
                  className={`h-0.5 flex-1 mx-3 rounded-full ${
                    getStepState(i + 1, handoff.status) === 'future'
                      ? 'bg-[var(--border)]'
                      : getStepState(i + 1, handoff.status) === 'expired'
                        ? 'bg-red-500/40'
                        : 'bg-[var(--brand)]'
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
    <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 mb-5">
      <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-4">Session Context</h3>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm mb-4">
        <div>
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Tool</span>
          <span className="text-[var(--text-secondary)]">{summary.tool}</span>
        </div>
        <div>
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Model</span>
          <span className="text-[var(--text-secondary)]">{summary.model ? abbreviateModel(summary.model) : '-'}</span>
        </div>
        <div>
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Messages</span>
          <span className="text-[var(--text-secondary)]">{summary.message_count}</span>
        </div>
        <div>
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Commands</span>
          <span className="text-[var(--text-secondary)]">{summary.commands_executed}</span>
        </div>
      </div>

      {/* Files modified */}
      {summary.files_modified.length > 0 && (
        <div className="mb-4">
          <span className="text-[var(--text-tertiary)] text-sm block mb-1.5">Files modified</span>
          <div className="flex flex-wrap gap-1.5">
            {filesCapped.map((f) => (
              <span
                key={f}
                className="text-xs bg-[var(--surface)] text-[var(--text-secondary)] px-2 py-1 rounded-md border border-[var(--border)] truncate max-w-[200px]"
                title={f}
              >
                {f}
              </span>
            ))}
            {extraFiles > 0 && (
              <span className="text-xs text-[var(--text-tertiary)] px-2 py-1">
                +{extraFiles} more
              </span>
            )}
          </div>
        </div>
      )}

      {/* Test results */}
      {hasTests && (
        <div className="mb-4">
          <span className="text-[var(--text-tertiary)] text-sm block mb-1.5">Tests</span>
          <div className="flex gap-2">
            <span className="text-xs px-2 py-1 rounded-md bg-green-500/10 text-green-500 border border-green-500/30">
              {summary.tests_passed} passed
            </span>
            {summary.tests_failed > 0 && (
              <span className="text-xs px-2 py-1 rounded-md bg-red-500/10 text-red-500 border border-red-500/30">
                {summary.tests_failed} failed
              </span>
            )}
          </div>
        </div>
      )}

      {/* Errors */}
      {summary.errors_encountered.length > 0 && (
        <div className="mb-4">
          <span className="text-[var(--text-tertiary)] text-sm block mb-1.5">Errors</span>
          <div className="space-y-1.5">
            {summary.errors_encountered.map((err, i) => (
              <p key={i} className="text-xs text-red-500 bg-red-500/5 px-2.5 py-1.5 rounded-md border border-red-500/20 truncate">
                {err}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* Last activity */}
      {lastMessage && (
        <div>
          <span className="text-[var(--text-tertiary)] text-sm block mb-1.5">Last activity</span>
          <p className="text-sm text-[var(--text-secondary)] bg-[var(--surface)] px-3 py-2 rounded-lg border border-[var(--border)] line-clamp-3">
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
    pending: 'bg-yellow-500/10 text-yellow-500 border-yellow-500/30',
    claimed: 'bg-green-500/10 text-green-500 border-green-500/30',
    expired: 'bg-neutral-500/10 text-neutral-400 border-neutral-500/30',
  };
  return (
    <span className={`px-2.5 py-1 text-xs font-medium border rounded-full ${styles[status]}`}>
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
      void queryClient.invalidateQueries({ queryKey: ['handoff', id, 'summary'] });
      void queryClient.invalidateQueries({ queryKey: ['handoffs'] });
      void queryClient.invalidateQueries({ queryKey: ['handoffs-inbox'] });
    },
  });

  if (isLoading) {
    return <div className="p-8 text-[var(--text-tertiary)]">Loading handoff...</div>;
  }

  if (error || !handoff) {
    return (
      <div className="p-8">
        <button onClick={() => navigate('/handoffs')} className="text-[var(--brand)] text-sm mb-4 hover:underline">
          &larr; Back to Handoffs
        </button>
        <p className="text-red-500">Failed to load handoff: {String(error)}</p>
      </div>
    );
  }

  const isRecipient = handoff.status === 'pending';
  const effectiveSessionId = handoff.recipient_session_id || handoff.session_id;
  const pullCommand = `sfs pull-handoff ${handoff.id}`;

  return (
    <div className="max-w-3xl mx-auto px-4 py-6">
      <button
        onClick={() => navigate('/handoffs')}
        className="text-[var(--brand)] text-[14px] mb-5 hover:underline"
      >
        &larr; Back to Handoffs
      </button>

      {/* Status stepper */}
      <HandoffStepper handoff={handoff} />

      {/* Session context from summary */}
      {summary && <SessionContextCard summary={summary} />}

      {/* Session preview card */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 mb-5">
        <div className="flex items-start justify-between mb-4">
          <h2 className="text-lg font-semibold text-[var(--text-primary)] break-words">
            {handoff.session_title || 'Untitled session'}
          </h2>
          <StatusBadge status={handoff.status} />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
          <div>
            <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Tool</span>
            <span className="text-[var(--text-secondary)]">{handoff.session_tool}</span>
          </div>
          <div>
            <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Model</span>
            <span className="text-[var(--text-secondary)]">{abbreviateModel(handoff.session_model_id)}</span>
          </div>
          <div>
            <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Messages</span>
            <span className="text-[var(--text-secondary)]">{handoff.session_message_count}</span>
          </div>
          <div>
            <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Tokens</span>
            <span className="text-[var(--text-secondary)]">{formatTokens(handoff.session_total_tokens ?? 0)}</span>
          </div>
        </div>
      </div>

      {/* Sender message */}
      {handoff.message && (
        <div className="border-l-2 border-[var(--brand)]/50 pl-4 mb-5">
          <p className="text-sm text-[var(--text-tertiary)] mb-1">Message from {handoff.sender_email}</p>
          <p className="text-sm text-[var(--text-secondary)] whitespace-pre-wrap">{handoff.message}</p>
        </div>
      )}

      {/* Claim action */}
      {isRecipient && (
        <div className="mb-5">
          <button
            onClick={() => claimMutation.mutate()}
            disabled={claimMutation.isPending}
            className="bg-[var(--brand)] text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
          >
            {claimMutation.isPending ? 'Claiming...' : 'Claim this handoff'}
          </button>
          {claimMutation.isError && (
            <p className="text-red-500 text-sm mt-2">
              Failed to claim: {String(claimMutation.error)}
            </p>
          )}
        </div>
      )}

      {/* CLI pull command */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-4 mb-5">
        <p className="text-sm text-[var(--text-tertiary)] mb-2">Pull via CLI</p>
        <div className="flex items-center gap-2">
          <code className="text-sm text-[var(--text-secondary)] bg-[var(--surface)] border border-[var(--border)] px-3 py-2 rounded-lg flex-1 truncate">
            {pullCommand}
          </code>
          <CopyButton text={pullCommand} label="Copy" />
        </div>
      </div>

      {/* Participants and timestamps */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 mb-5">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">From</span>
            <span className="text-[var(--text-secondary)]">{handoff.sender_email}</span>
          </div>
          <div>
            <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">To</span>
            <span className="text-[var(--text-secondary)]">{handoff.recipient_email}</span>
          </div>
          <div>
            <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Sent</span>
            <span className="text-[var(--text-secondary)]"><RelativeDate iso={handoff.created_at} /></span>
          </div>
          {handoff.claimed_at && (
            <div>
              <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Claimed</span>
              <span className="text-[var(--text-secondary)]"><RelativeDate iso={handoff.claimed_at} /></span>
            </div>
          )}
        </div>
      </div>

      {/* Link to full session if claimed */}
      {handoff.status === 'claimed' && (
        <Link
          to={`/sessions/${effectiveSessionId}`}
          className="text-[var(--brand)] text-sm hover:underline"
        >
          View full session &rarr;
        </Link>
      )}
    </div>
  );
}
