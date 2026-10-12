import { useState } from 'react';
import { ApiError, type SkillInstallRequest } from '../api/client';
import { useMe } from '../hooks/useMe';
import {
  useApproveSkillInstallRequest,
  useCancelSkillInstallRequest,
  useChallengeSkillInstallRequest,
  useRevokeSkillInstallRequest,
  useSkillInstallRequests,
} from '../hooks/useMarketplace';
import { useToast } from '../hooks/useToast';
import { Badge } from '../components/Badge';
import RelativeDate from '../components/RelativeDate';
import { Button, Input } from '../components/ui';

interface MarketplaceTabProps { projectId: string }

function requestVariant(status: string): 'default' | 'success' | 'warning' | 'danger' {
  if (status === 'approved' || status === 'applied') return 'success';
  if (status === 'pending') return 'warning';
  if (status === 'rejected') return 'danger';
  return 'default';
}

/** Every requested capability other than plain instruction text is a permission grant. */
export const ELEVATED_CAPABILITY_LABELS: Record<string, string> = {
  scripts: 'Scripts — executable assets the agent may invoke',
  hooks: 'Hooks / allowed-tools — commands run or tools pre-approved on invocation',
  mcp: 'MCP servers — local processes or remote origins',
  assets: 'Assets — bounded non-instruction files',
};

function elevatedCapabilities(row: SkillInstallRequest): string[] {
  return row.requested_capabilities.filter((capability) => capability !== 'instructions');
}

function ApprovalRow({
  row,
  isRequester,
  onApprove,
  onChallenge,
  pending,
  challengePending,
}: {
  row: SkillInstallRequest;
  isRequester: boolean;
  onApprove: (approvedCapabilities: string[], confirmationCode?: string) => void;
  onChallenge: () => void;
  pending: boolean;
  challengePending: boolean;
}) {
  const elevated = elevatedCapabilities(row);
  const [confirmed, setConfirmed] = useState<string[]>([]);
  const [code, setCode] = useState('');
  const allConfirmed = elevated.every((capability) => confirmed.includes(capability));
  const codeReady = !isRequester || code.trim().length > 0;
  return (
    <div className="flex flex-col gap-2">
      {elevated.length > 0 && (
        <fieldset className="rounded-lg border border-warning/40 bg-[rgba(240,192,64,0.06)] p-3">
          <legend className="px-1 text-xs font-semibold text-warning">Elevated capabilities requested — confirm each one</legend>
          {elevated.map((capability) => (
            <label key={capability} className="mt-1 flex items-start gap-2 text-xs text-text-primary">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={confirmed.includes(capability)}
                onChange={(event) => setConfirmed((current) =>
                  event.target.checked ? [...current, capability] : current.filter((item) => item !== capability),
                )}
                aria-label={`Confirm ${capability}`}
              />
              <span>
                <span className="font-mono">{capability}</span> · {ELEVATED_CAPABILITY_LABELS[capability] || 'Elevated capability'}
                {capability === 'hooks' && ((row.hook_events?.length ?? 0) > 0 || (row.allowed_tools?.length ?? 0) > 0) && (
                  <span className="mt-0.5 block font-mono text-2xs text-text-secondary" data-testid={`capability-detail-${capability}`}>
                    {row.hook_events?.length ? `hooks: ${row.hook_events.join(', ')}` : ''}
                    {row.hook_events?.length && row.allowed_tools?.length ? ' · ' : ''}
                    {row.allowed_tools?.length ? `pre-approved: ${row.allowed_tools.join(', ')}` : ''}
                  </span>
                )}
              </span>
            </label>
          ))}
        </fieldset>
      )}
      {isRequester && (
        <div className="rounded-lg border border-border bg-bg-sunken p-3">
          <p className="text-xs text-text-secondary">You filed this request. Approving it yourself requires the single-use code emailed to your account.</p>
          <div className="mt-2 flex flex-wrap items-end gap-2">
            <Button size="sm" variant="ghost" type="button" onClick={onChallenge} loading={challengePending}>
              {row.approval_challenge_pending ? 'Resend confirmation code' : 'Send confirmation code'}
            </Button>
            <Input
              title="Confirmation code"
              aria-label="Confirmation code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              maxLength={16}
              placeholder="8-character code"
              autoComplete="one-time-code"
            />
          </div>
        </div>
      )}
      <Button
        size="sm"
        onClick={() => onApprove(confirmed, isRequester ? code.trim() : undefined)}
        disabled={!allConfirmed || !codeReady}
        loading={pending}
      >
        Approve exact hash
      </Button>
    </div>
  );
}

export default function MarketplaceTab({ projectId }: MarketplaceTabProps) {
  const requests = useSkillInstallRequests(projectId);
  const approve = useApproveSkillInstallRequest(projectId);
  const challenge = useChallengeSkillInstallRequest(projectId);
  const revoke = useRevokeSkillInstallRequest(projectId);
  const cancel = useCancelSkillInstallRequest(projectId);
  const me = useMe();
  const { addToast } = useToast();
  const canApprove = requests.data?.can_approve === true;

  async function sendChallenge(requestId: string) {
    try {
      const result = await challenge.mutateAsync(requestId);
      addToast(
        result.delivery === 'email' ? 'success' : 'error',
        result.delivery === 'email'
          ? 'Confirmation code sent to your email. It expires in 10 minutes.'
          : 'Email delivery is not configured on this server; ask another owner or admin to approve.',
      );
    } catch (error) {
      if (error instanceof ApiError && error.code === 'confirmation_delivery_failed') {
        addToast('error', 'The confirmation code could not be delivered to your email. Ask another owner or admin to approve, or retry later.');
        return;
      }
      addToast('error', `Could not send confirmation code: ${String(error)}`);
    }
  }

  async function revokeGrant(requestId: string) {
    try {
      const result = await revoke.mutateAsync(requestId);
      addToast('success', result.warning ? `Grant revoked. ${result.warning}` : 'Grant revoked; consent receipts invalidated.');
    } catch (error) {
      addToast('error', `Revoke failed: ${String(error)}`);
    }
  }

  async function decide(
    requestId: string,
    action: 'approve' | 'cancel',
    approvedCapabilities: string[] = [],
    confirmationCode?: string,
  ) {
    try {
      if (action === 'approve') await approve.mutateAsync({ requestId, approvedCapabilities, confirmationCode });
      else await cancel.mutateAsync(requestId);
      addToast('success', `Install request ${action === 'approve' ? 'approved' : 'cancelled'}.`);
    } catch (error) {
      addToast('error', `${action === 'approve' ? 'Approval' : 'Cancellation'} failed: ${String(error)}`);
    }
  }

  if (requests.isLoading) return <p className="p-4 text-sm text-text-tertiary">Loading install requests…</p>;
  if (requests.error) return <p role="alert" className="p-4 text-sm text-danger">Failed to load install requests: {String(requests.error)}</p>;
  const rows = requests.data?.items || [];
  return (
    <section aria-labelledby="project-marketplace-heading" className="space-y-4">
      <div>
        <h2 id="project-marketplace-heading" className="text-lg font-semibold text-text-primary">Marketplace approvals</h2>
        <p className="mt-1 text-sm text-text-secondary">Exact package hashes approved for this project. Approval permits instruction retrieval only; devices still control local writes and executable activation.</p>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg border border-border bg-bg-sunken p-6 text-center">
          <p className="text-sm font-medium text-text-primary">No install requests</p>
          <p className="mt-1 text-xs text-text-tertiary">Browse Marketplace and request a package for this project.</p>
        </div>
      ) : (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
          {rows.map((row) => (
            <li key={row.id} className="flex flex-col gap-3 bg-bg-elevated p-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm text-text-primary">{row.version_id}</span>
                  <Badge label={row.status} variant={requestVariant(row.status)} tint />
                  <Badge label={row.target_scope} />
                  {row.requested_via === 'mcp' && <Badge label="filed via MCP" variant="warning" tint />}
                </div>
                <p className="mt-2 truncate font-mono text-xs text-text-tertiary" title={row.content_hash}>{row.content_hash}</p>
                <p className="mt-1 text-xs text-text-tertiary">{row.target_tools.join(', ') || 'all tools'} · requested <RelativeDate iso={row.created_at} /></p>
                <p className="mt-2 flex flex-wrap items-center gap-1 text-xs" aria-label="Requested capabilities">
                  <span className="text-text-tertiary">Capabilities:</span>
                  {row.requested_capabilities.map((capability) => (
                    <Badge
                      key={capability}
                      label={capability}
                      variant={capability === 'instructions' ? 'default' : 'warning'}
                      tint
                    />
                  ))}
                </p>
              </div>
              {(row.status === 'approved' || row.status === 'applied') && (canApprove || row.requested_by_user_id === me.data?.user_id) && (
                <div className="flex shrink-0 gap-2">
                  <Button size="sm" variant="ghost" onClick={() => void revokeGrant(row.id)} loading={revoke.isPending}>Revoke grant</Button>
                </div>
              )}
              {row.status === 'pending' && (
                <div className="flex shrink-0 flex-col items-end gap-2 sm:flex-row sm:items-start">
                  {(canApprove || row.requested_by_user_id === me.data?.user_id) && (
                    <Button size="sm" variant="ghost" onClick={() => void decide(row.id, 'cancel')} loading={cancel.isPending}>Cancel</Button>
                  )}
                  {canApprove && (
                    <ApprovalRow
                      row={row}
                      isRequester={row.requested_by_user_id === me.data?.user_id}
                      pending={approve.isPending}
                      challengePending={challenge.isPending}
                      onChallenge={() => void sendChallenge(row.id)}
                      onApprove={(approvedCapabilities, confirmationCode) =>
                        void decide(row.id, 'approve', approvedCapabilities, confirmationCode)}
                    />
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
