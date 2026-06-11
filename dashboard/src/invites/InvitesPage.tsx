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
 *
 * Phase 3 restyle: onto Card, Button, Textarea primitives.
 */

import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { useToast } from '../hooks/useToast';
import { useAcceptInvite, useDeclineInvite, useMyInvites } from './useInvites';
import { Button, Card, Textarea } from '../components/ui';
import { Badge } from '../components/Badge';

export default function InvitesPage() {
  const invites = useMyInvites();
  const accept = useAcceptInvite();
  const decline = useDeclineInvite();
  const { addToast } = useToast();
  const [params] = useSearchParams();
  const highlight = params.get('highlight');
  const [decliningId, setDecliningId] = useState<string | null>(null);
  const [declineReason, setDeclineReason] = useState('');

  if (invites.isLoading) return <p className="text-[var(--text-tertiary)] p-4 text-sm">Loading invites…</p>;
  if (invites.error)
    return <p role="alert" className="text-[var(--danger)] p-4 text-sm">Failed to load invites: {String(invites.error)}</p>;

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
    <section aria-labelledby="invites-heading" className="max-w-2xl mx-auto px-4 py-6">
      <h2 id="invites-heading" className="text-xl font-bold tracking-tight text-[var(--text-primary)] mb-2">Organization invites</h2>
      <p className="text-sm text-[var(--text-tertiary)] mb-5">
        Invites sent to your email address. Accept to join the org, or decline
        if you weren't expecting it.
      </p>

      {rows.length === 0 ? (
        <div className="text-center py-12">
          <svg className="mx-auto mb-3 opacity-30" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
            <circle cx="9" cy="7" r="4" />
            <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
            <path d="M16 3.13a4 4 0 0 1 0 7.75" />
          </svg>
          <p className="text-[15px] font-semibold text-[var(--text-primary)] mb-1">No pending invites</p>
          <p className="text-[13px] text-[var(--text-tertiary)]">
            You don't have any pending organization invitations.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {rows.map((inv) => {
            const isHighlight = highlight === inv.invite_id;
            const isDeclining = decliningId === inv.invite_id;
            return (
              <Card
                key={inv.invite_id}
                className="p-4"
                style={isHighlight ? { boxShadow: '0 0 0 2px var(--brand-glow)' } : undefined}
                data-testid={`invite-${inv.invite_id}`}
              >
                <div className="flex items-center justify-between gap-3 mb-2">
                  <div>
                    <strong className="text-[var(--text-primary)]">{inv.org_name}</strong>
                    <span className="text-[var(--text-tertiary)] text-sm ml-2">
                      role: <Badge variant="default" label={inv.role} size="sm" />
                    </span>
                  </div>
                </div>
                <div className="text-xs text-[var(--text-tertiary)] mb-3">
                  Invited by {inv.invited_by_email} on{' '}
                  {new Date(inv.created_at).toLocaleDateString()}. Expires{' '}
                  {new Date(inv.expires_at).toLocaleDateString()}.
                </div>
                {isDeclining ? (
                  <div className="space-y-3">
                    <Textarea
                      title="Decline reason (optional, max 1000 chars)"
                      id={`reason-${inv.invite_id}`}
                      value={declineReason}
                      onChange={(e) => setDeclineReason(e.target.value)}
                      maxLength={1000}
                      rows={2}
                    />
                    <div className="flex gap-2">
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => handleDecline(inv.invite_id, inv.org_name)}
                        disabled={busy}
                        aria-label={`Confirm decline invite to ${inv.org_name}`}
                      >
                        Confirm decline
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => {
                          setDecliningId(null);
                          setDeclineReason('');
                        }}
                        disabled={busy}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      onClick={() => handleAccept(inv.invite_id, inv.org_name)}
                      disabled={busy}
                      aria-label={`Accept invite to ${inv.org_name}`}
                    >
                      Accept
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => setDecliningId(inv.invite_id)}
                      disabled={busy}
                      aria-label={`Decline invite to ${inv.org_name}`}
                    >
                      Decline
                    </Button>
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}
