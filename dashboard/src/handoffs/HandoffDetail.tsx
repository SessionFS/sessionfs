import { useParams, useNavigate, Link } from 'react-router-dom';
import { useHandoff } from '../hooks/useHandoffs';
import { useAuth } from '../auth/AuthContext';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { formatTokens } from '../utils/tokens';
import { abbreviateModel } from '../utils/models';
import CopyButton from '../components/CopyButton';
import RelativeDate from '../components/RelativeDate';
import type { HandoffDetail as HandoffDetailType } from '../api/client';

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

export default function HandoffDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const { data: handoff, isLoading, error } = useHandoff(id!);

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
            <span className="text-text-secondary">{handoff.session_source_tool}</span>
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
            <span className="text-text-secondary">{formatTokens(handoff.session_total_tokens)}</span>
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
