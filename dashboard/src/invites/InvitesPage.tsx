/**
 * v0.10.22 — Org-invite acceptance page (tk_6afbcfefe5804c1d).
 *
 * Lists pending OrgInvites for the logged-in user (matched on email)
 * with Accept and Decline controls. Used by:
 *   - direct nav from the header / a new-invite banner
 *   - the email accept link, which lands on /invites and highlights
 *     a specific invite via the ?highlight=<invite_id> query string
 *
 * Backend invariants we rely on:
 *   - /api/v1/org/invites/me already filters out accepted, declined,
 *     and expired rows; no client-side filtering needed.
 *   - Accept is atomic on the server with seat re-check; we surface
 *     server's structured error envelope back to the toast.
 */

import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { useToast } from '../hooks/useToast';
import { useAcceptInvite, useDeclineInvite, useMyInvites } from './useInvites';

export default function InvitesPage() {
  const invites = useMyInvites();
  const accept = useAcceptInvite();
  const decline = useDeclineInvite();
  const { addToast } = useToast();
  const [params] = useSearchParams();
  const highlight = params.get('highlight');
  const [decliningId, setDecliningId] = useState<string | null>(null);
  const [declineReason, setDeclineReason] = useState('');

  if (invites.isLoading) return <p>Loading invites…</p>;
  if (invites.error)
    return <p role="alert">Failed to load invites: {String(invites.error)}</p>;

  const rows = invites.data?.invites ?? [];
  const busy = accept.isPending || decline.isPending;

  const handleAccept = (inviteId: string, orgName: string) => {
    accept.mutate(
      { inviteId },
      {
        onSuccess: () => addToast('success', `Joined ${orgName}`),
        onError: (err) => addToast('error', `Accept failed: ${err.message}`),
      },
    );
  };

  const handleDecline = (inviteId: string, orgName: string) => {
    decline.mutate(
      { inviteId, reason: declineReason.trim() || undefined },
      {
        onSuccess: () => {
          addToast('success', `Declined invite to ${orgName}`);
          setDecliningId(null);
          setDeclineReason('');
        },
        onError: (err) => addToast('error', `Decline failed: ${err.message}`),
      },
    );
  };

  return (
    <section aria-labelledby="invites-heading">
      <h2 id="invites-heading">Organization invites</h2>
      <p className="text-[var(--text-tertiary)]">
        Invites sent to your email address. Accept to join the org, or decline
        if you weren't expecting it.
      </p>
      {rows.length === 0 ? (
        <p className="text-[var(--text-tertiary)]">No pending invites.</p>
      ) : (
        <ul>
          {rows.map((inv) => {
            const isHighlight = highlight === inv.invite_id;
            const isDeclining = decliningId === inv.invite_id;
            return (
              <li
                key={inv.invite_id}
                data-testid={`invite-${inv.invite_id}`}
                style={
                  isHighlight
                    ? { outline: '2px solid var(--accent-primary)', padding: '8px' }
                    : undefined
                }
              >
                <div>
                  <strong>{inv.org_name}</strong>
                  <span> — role: {inv.role}</span>
                </div>
                <div className="text-[var(--text-tertiary)] text-xs">
                  Invited by {inv.invited_by_email} on{' '}
                  {new Date(inv.created_at).toLocaleDateString()}. Expires{' '}
                  {new Date(inv.expires_at).toLocaleDateString()}.
                </div>
                {isDeclining ? (
                  <div>
                    <label
                      htmlFor={`reason-${inv.invite_id}`}
                      className="text-xs text-[var(--text-tertiary)]"
                    >
                      Decline reason (optional, max 1000 chars)
                    </label>
                    <textarea
                      id={`reason-${inv.invite_id}`}
                      value={declineReason}
                      onChange={(e) => setDeclineReason(e.target.value)}
                      maxLength={1000}
                      rows={2}
                    />
                    <button
                      onClick={() => handleDecline(inv.invite_id, inv.org_name)}
                      disabled={busy}
                      aria-label={`Confirm decline invite to ${inv.org_name}`}
                    >
                      Confirm decline
                    </button>
                    <button
                      onClick={() => {
                        setDecliningId(null);
                        setDeclineReason('');
                      }}
                      disabled={busy}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div>
                    <button
                      onClick={() => handleAccept(inv.invite_id, inv.org_name)}
                      disabled={busy}
                      aria-label={`Accept invite to ${inv.org_name}`}
                    >
                      Accept
                    </button>
                    <button
                      onClick={() => setDecliningId(inv.invite_id)}
                      disabled={busy}
                      aria-label={`Decline invite to ${inv.org_name}`}
                    >
                      Decline
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
