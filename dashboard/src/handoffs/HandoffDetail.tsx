import { useParams, useNavigate, Link } from 'react-router-dom';
import { useHandoff, useHandoffSummary } from '../hooks/useHandoffs';
import { useAuth } from '../auth/AuthContext';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { formatTokens } from '../utils/tokens';
import { abbreviateModel } from '../utils/models';
import CopyButton from '../components/CopyButton';
import RelativeDate from '../components/RelativeDate';
import { Badge } from '../components/Badge';
import { Card, Button } from '../components/ui';
import type { HandoffDetail as HandoffDetailType, HandoffSessionSummary } from '../api/client';

/** Map handoff statuses to Badge variants. */
const STATUS_VARIANT: Record<string, 'warning' | 'success' | 'danger'> = {
  pending: 'warning',
  claimed: 'success',
  expired: 'danger',
};

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
    <Card level="elevated" className="p-5 mb-5">
      <div className="flex items-center">
        {steps.map((label, i) => {
          const state = getStepState(i, handoff.status);
          const timestamp = getStepTimestamp(i, handoff);

          return (
            <div key={label} className="flex items-center flex-1 last:flex-none">
              <div className="flex flex-col items-center">
                <div
                  className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-medium transition-colors ${
                    state === 'completed'
                      ? 'bg-[var(--brand)] text-white'
                      : state === 'current'
                        ? 'bg-[var(--brand)] text-white shadow-[0_0_0_4px_var(--bg-elevated),0_0_0_6px_var(--brand)]'
                        : state === 'expired'
                          ? 'bg-[var(--danger)] text-white'
                          : 'border-2 border-[var(--border)] text-[var(--text-tertiary)]'
                  }`}
                  style={state === 'future' ? { backgroundColor: 'var(--bg-elevated)' } : undefined}
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
                          ? 'text-[var(--danger)]'
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

              {i < steps.length - 1 && (
                <div
                  className={`h-0.5 flex-1 mx-3 rounded-full ${
                    getStepState(i + 1, handoff.status) === 'future'
                      ? 'bg-[var(--border)]'
                      : getStepState(i + 1, handoff.status) === 'expired'
                        ? ''
                        : 'bg-[var(--brand)]'
                  }`}
                  style={
                    getStepState(i + 1, handoff.status) === 'expired'
                      ? { backgroundColor: 'rgba(240,64,96,0.4)' }
                      : undefined
                  }
                />
              )}
            </div>
          );
        })}
      </div>
    </Card>
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
    <Card level="elevated" className="p-5 mb-5">
      <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-4">Session Context</h3>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm mb-4">
        <div>
          <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Tool</span>
          <span className="text-[var(--text-secondary)]">{summary.tool}</span>
        </div>
        <div>
          <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Model</span>
          <span className="text-[var(--text-secondary)]">{summary.model ? abbreviateModel(summary.model) : '-'}</span>
        </div>
        <div>
          <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Messages</span>
          <span className="text-[var(--text-secondary)]">{summary.message_count}</span>
        </div>
        <div>
          <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Commands</span>
          <span className="text-[var(--text-secondary)]">{summary.commands_executed}</span>
        </div>
      </div>

      {/* Files modified */}
      {summary.files_modified.length > 0 && (
        <div className="mb-4">
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-1.5">Files modified</span>
          <div className="flex flex-wrap gap-1.5">
            {filesCapped.map((f) => (
              <span
                key={f}
                className="text-xs px-2 py-1 rounded-md border border-[var(--border)] truncate max-w-[200px]"
                style={{ backgroundColor: 'var(--surface)', color: 'var(--text-secondary)' }}
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
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-1.5">Tests</span>
          <div className="flex gap-2">
            <Badge variant="success" label={`${summary.tests_passed} passed`} size="sm" />
            {summary.tests_failed > 0 && (
              <Badge variant="danger" label={`${summary.tests_failed} failed`} size="sm" />
            )}
          </div>
        </div>
      )}

      {/* Errors */}
      {summary.errors_encountered.length > 0 && (
        <div className="mb-4">
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-1.5">Errors</span>
          <div className="space-y-1.5">
            {summary.errors_encountered.map((err, i) => (
              <p key={i} className="text-xs truncate px-2.5 py-1.5 rounded-md border" style={{ color: 'var(--danger)', backgroundColor: 'rgba(240,64,96,0.05)', borderColor: 'rgba(240,64,96,0.2)' }}>
                {err}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* Last activity */}
      {lastMessage && (
        <div>
          <span className="text-[13px] text-[var(--text-tertiary)] block mb-1.5">Last activity</span>
          <p className="text-sm text-[var(--text-secondary)] px-3 py-2 rounded-lg border border-[var(--border)] line-clamp-3" style={{ backgroundColor: 'var(--surface)' }}>
            {lastMessage}
          </p>
        </div>
      )}
    </Card>
  );
}

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
    return <div className="p-8 text-[var(--text-tertiary)] text-sm">Loading handoff…</div>;
  }

  if (error || !handoff) {
    return (
      <div className="p-8">
        <Button variant="ghost" size="sm" onClick={() => navigate('/handoffs')} className="mb-4">
          ← Back to Handoffs
        </Button>
        <p className="text-[var(--danger)] text-sm">Failed to load handoff: {String(error)}</p>
      </div>
    );
  }

  const isRecipient = handoff.status === 'pending';
  const effectiveSessionId = handoff.recipient_session_id || handoff.session_id;
  const pullCommand = `sfs pull-handoff ${handoff.id}`;

  return (
    <div className="max-w-3xl mx-auto px-4 py-6">
      <Button variant="ghost" size="sm" onClick={() => navigate('/handoffs')} className="mb-5">
        ← Back to Handoffs
      </Button>

      {/* Status stepper */}
      <HandoffStepper handoff={handoff} />

      {/* Session context from summary */}
      {summary && <SessionContextCard summary={summary} />}

      {/* Session preview card */}
      <Card level="elevated" className="p-5 mb-5">
        <div className="flex items-start justify-between mb-4">
          <h2 className="text-lg font-semibold text-[var(--text-primary)] break-words">
            {handoff.session_title || 'Untitled session'}
          </h2>
          <Badge
            variant={STATUS_VARIANT[handoff.status] ?? 'default'}
            tint
            label={handoff.status}
            size="sm"
          />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
          <div>
            <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Tool</span>
            <span className="text-[var(--text-secondary)]">{handoff.session_tool}</span>
          </div>
          <div>
            <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Model</span>
            <span className="text-[var(--text-secondary)]">{abbreviateModel(handoff.session_model_id)}</span>
          </div>
          <div>
            <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Messages</span>
            <span className="text-[var(--text-secondary)]">{handoff.session_message_count}</span>
          </div>
          <div>
            <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Tokens</span>
            <span className="text-[var(--text-secondary)]">{formatTokens(handoff.session_total_tokens ?? 0)}</span>
          </div>
        </div>
      </Card>

      {/* Sender message */}
      {handoff.message && (
        <div className="mb-5 pl-4" style={{ borderLeft: '2px solid var(--brand-glow)' }}>
          <p className="text-micro text-[var(--text-tertiary)] mb-1">Message from {handoff.sender_email}</p>
          <p className="text-sm text-[var(--text-secondary)] whitespace-pre-wrap">{handoff.message}</p>
        </div>
      )}

      {/* Claim action */}
      {isRecipient && (
        <div className="mb-5">
          <Button onClick={() => claimMutation.mutate()} loading={claimMutation.isPending}>
            {claimMutation.isPending ? 'Claiming…' : 'Claim this handoff'}
          </Button>
          {claimMutation.isError && (
            <p className="text-[var(--danger)] text-sm mt-2">
              Failed to claim: {String(claimMutation.error)}
            </p>
          )}
        </div>
      )}

      {/* CLI pull command */}
      <Card level="elevated" className="p-4 mb-5">
        <p className="text-sm text-[var(--text-tertiary)] mb-2">Pull via CLI</p>
        <div className="flex items-center gap-2">
          <code
            className="text-sm px-3 py-2 rounded-lg border border-[var(--border)] flex-1 truncate"
            style={{ backgroundColor: 'var(--surface)', color: 'var(--text-secondary)' }}
          >
            {pullCommand}
          </code>
          <CopyButton text={pullCommand} label="Copy" />
        </div>
      </Card>

      {/* Participants and timestamps */}
      <Card level="elevated" className="p-5 mb-5">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">From</span>
            <span className="text-[var(--text-secondary)]">{handoff.sender_email}</span>
          </div>
          <div>
            <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">To</span>
            <span className="text-[var(--text-secondary)]">{handoff.recipient_email}</span>
          </div>
          <div>
            <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Sent</span>
            <span className="text-[var(--text-secondary)]"><RelativeDate iso={handoff.created_at} /></span>
          </div>
          {handoff.claimed_at && (
            <div>
              <span className="text-micro text-[var(--text-tertiary)] block mb-0.5">Claimed</span>
              <span className="text-[var(--text-secondary)]"><RelativeDate iso={handoff.claimed_at} /></span>
            </div>
          )}
        </div>
      </Card>

      {/* Link to full session if claimed */}
      {handoff.status === 'claimed' && (
        <Link
          to={`/sessions/${effectiveSessionId}`}
          className="text-[var(--brand)] text-sm hover:underline"
        >
          View full session →
        </Link>
      )}
    </div>
  );
}
