import { useState } from 'react';
import {
  ApiError,
  type ApprovalBinding,
  type CapabilityChange,
  type SkillInstallRequest,
  type UpdateDiffDetail,
} from '../api/client';
import { useMarketplaceDisclosure } from '../hooks/useMarketplaceDisclosure';
import { useMe } from '../hooks/useMe';
import {
  useApproveSkillInstallRequest,
  useCancelSkillInstallRequest,
  useChallengeSkillInstallRequest,
  useRevocationIndex,
  useRevokeSkillInstallRequest,
  useSkillInstallRequests,
} from '../hooks/useMarketplace';
import { useToast } from '../hooks/useToast';
import { Badge } from '../components/Badge';
import { versionState } from '../marketplace/moderationState';
import { ModerationStateBadge, RevocationIncompleteNotice } from '../marketplace/parts';
import { DisclosureCard, UpdateDiff } from '../marketplace/DisclosureCard';
import RelativeDate from '../components/RelativeDate';
import { Button, Input } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';

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

/** Server refusal of an approval that updates an earlier approved version (D2). */
interface Reapproval {
  update_class: UpdateDiffDetail['update_class'];
  widens: CapabilityChange[];
  narrows: CapabilityChange[];
  baseline_known: boolean;
  code_changes: { kind: string; change: string; path: string }[];
  /** The exact diff the update_digest binds; acknowledgement is impossible without it. */
  diff?: UpdateDiffDetail;
  /** Binds the acknowledgement to exactly this baseline, target and diff. */
  update_digest: string | null;
  /** True when an acknowledgement was refused because the diff changed since it was shown. */
  diff_changed: boolean;
}

function reapprovalFrom(error: unknown): Reapproval | null {
  if (!(error instanceof ApiError) || error.code !== 'reapproval_required') return null;
  const details = (error.details || {}) as Partial<Reapproval>;
  return {
    update_class: details.update_class === 'code' ? 'code' : 'widening',
    widens: Array.isArray(details.widens) ? details.widens : [],
    narrows: Array.isArray(details.narrows) ? details.narrows : [],
    baseline_known: details.baseline_known !== false,
    code_changes: Array.isArray(details.code_changes) ? details.code_changes : [],
    diff: details.diff && typeof details.diff === 'object' ? details.diff : undefined,
    update_digest: typeof details.update_digest === 'string' ? details.update_digest : null,
    diff_changed: details.diff_changed === true,
  };
}

/** Where the request installs: user and device grants land in user scope. */
function disclosureScope(row: SkillInstallRequest): 'project' | 'user' {
  return row.target_scope === 'project' ? 'project' : 'user';
}

function ApprovalRow({
  row,
  isRequester,
  onApprove,
  onChallenge,
  pending,
  challengePending,
  reapproval = null,
  resetToken = 0,
}: {
  row: SkillInstallRequest;
  isRequester: boolean;
  onApprove: (
    approvedCapabilities: string[],
    confirmationCode: string | undefined,
    binding: ApprovalBinding,
    reloadDisclosure: () => void,
  ) => void;
  onChallenge: () => void;
  pending: boolean;
  challengePending: boolean;
  reapproval?: Reapproval | null;
  /** Bumped by the parent on 409 disclosure_changed: clears every confirmation. */
  resetToken?: number;
}) {
  const elevated = elevatedCapabilities(row);
  const [code, setCode] = useState('');
  // The approver must see what is being approved: approval stays disabled
  // until the disclosure for these tools and this scope has loaded.
  const disclosure = useMarketplaceDisclosure(row.version_id, row.target_tools, disclosureScope(row));
  // Confirmations belong to the exact card and diff that were reviewed: a
  // new disclosure digest, a different re-approval diff, or a
  // disclosure_changed refusal starts the review over.
  const reviewKey = `${disclosure.data?.disclosure_digest ?? ''}|${reapproval ? JSON.stringify(reapproval) : ''}|${resetToken}`;
  const [review, setReview] = useState<{ key: string; confirmed: string[]; acknowledged: boolean }>({
    key: reviewKey,
    confirmed: [],
    acknowledged: false,
  });
  const current = review.key === reviewKey ? review : { key: reviewKey, confirmed: [] as string[], acknowledged: false };
  const confirmed = current.confirmed;
  const acknowledged = current.acknowledged;
  const setConfirmed = (update: (items: string[]) => string[]) =>
    setReview((previous) => {
      const base = previous.key === reviewKey ? previous : { key: reviewKey, confirmed: [] as string[], acknowledged: false };
      return { ...base, confirmed: update(base.confirmed) };
    });
  const setAcknowledged = (value: boolean) =>
    setReview((previous) => {
      const base = previous.key === reviewKey ? previous : { key: reviewKey, confirmed: [] as string[], acknowledged: false };
      return { ...base, acknowledged: value };
    });
  const allConfirmed = elevated.every((capability) => confirmed.includes(capability));
  const codeReady = !isRequester || code.trim().length > 0;
  // Fail closed: without the diff object there is nothing to acknowledge, so
  // the acknowledgement cannot be given at all.
  const acknowledgementReady = !reapproval || (reapproval.diff != null && acknowledged);
  // Never approve against a disclosure that is being reloaded.
  const disclosureReady = !!disclosure.data && !disclosure.error && !disclosure.isFetching;
  return (
    <div className="flex flex-col gap-2">
      {disclosure.isLoading && <p className="text-xs text-text-tertiary">Loading install disclosure…</p>}
      {disclosure.error && (
        <p role="alert" className="text-xs text-danger">
          Install disclosure unavailable: {errorMessage(disclosure.error)}. Approval is disabled until it loads.
        </p>
      )}
      {disclosure.data && <DisclosureCard disclosure={disclosure.data} />}
      {reapproval && (
        <div className="rounded-lg border border-danger/40 bg-danger/6 p-3">
          <p className="text-xs font-semibold text-danger">
            {reapproval.update_class === 'widening'
              ? 'This version asks for more access than the version already approved here.'
              : 'This version changes executable content compared with the version already approved here.'}
          </p>
          {reapproval.diff ? (
            <>
              <div className="mt-2"><UpdateDiff diff={reapproval.diff} /></div>
              <label className="mt-2 flex items-start gap-2 text-xs text-text-primary">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                  aria-label="Acknowledge the update"
                />
                <span>I reviewed the changes above and approve them for this project.</span>
              </label>
            </>
          ) : (
            <p role="alert" className="mt-2 text-xs text-danger">
              The update diff is unavailable, so this update cannot be acknowledged. Reload and try again.
            </p>
          )}
        </div>
      )}
      {elevated.length > 0 && (
        <fieldset className="rounded-lg border border-warning/40 bg-warning/6 p-3">
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
        onClick={() =>
          onApprove(
            confirmed,
            isRequester ? code.trim() : undefined,
            {
              ...(reapproval
                ? reapproval.update_class === 'widening'
                  ? { widening: true }
                  : { codeChange: true }
                : {}),
              capabilityDigest: disclosure.data?.capability_digest ?? null,
              consentRuleVersion: disclosure.data?.consent_rule_version ?? 2,
              disclosureDigest: disclosure.data?.disclosure_digest ?? null,
              ...(reapproval?.update_digest ? { updateDigest: reapproval.update_digest } : {}),
            },
            () => void disclosure.refetch?.(),
          )}
        disabled={!allConfirmed || !codeReady || !acknowledgementReady || !disclosureReady}
        loading={pending}
      >
        {reapproval ? 'Approve update' : 'Approve exact hash'}
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
  const revocations = useRevocationIndex();
  const [reapprovals, setReapprovals] = useState<Record<string, Reapproval>>({});
  const [resetTokens, setResetTokens] = useState<Record<string, number>>({});

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
      addToast('error', `Send code failed: ${errorMessage(error)}`);
    }
  }

  async function revokeGrant(requestId: string) {
    try {
      const result = await revoke.mutateAsync(requestId);
      addToast('success', result.warning ? `Grant revoked. ${result.warning}` : 'Grant revoked; consent receipts invalidated.');
    } catch (error) {
      addToast('error', `Revoke failed: ${errorMessage(error)}`);
    }
  }

  async function decide(
    requestId: string,
    action: 'approve' | 'cancel',
    approvedCapabilities: string[] = [],
    confirmationCode?: string,
    acknowledgements?: ApprovalBinding,
    reloadDisclosure?: () => void,
  ) {
    try {
      if (action === 'approve') {
        await approve.mutateAsync({
          requestId,
          approvedCapabilities,
          confirmationCode,
          ...(acknowledgements ? { acknowledgements } : {}),
        });
      } else await cancel.mutateAsync(requestId);
      addToast('success', `Install request ${action === 'approve' ? 'approved' : 'cancelled'}.`);
    } catch (error) {
      if (action === 'approve' && error instanceof ApiError && error.code === 'disclosure_changed') {
        // What was shown is no longer current: reload it; the button stays
        // disabled until the fresh disclosure has loaded.
        reloadDisclosure?.();
        // The cached update diff may be stale too: drop it so the next
        // attempt fetches (and the approver reviews) the current one.
        setReapprovals((current) => {
          const { [requestId]: _stale, ...rest } = current;
          void _stale;
          return rest;
        });
        setResetTokens((current) => ({ ...current, [requestId]: (current[requestId] || 0) + 1 }));
        addToast('error', 'The package disclosure changed. Review the updated disclosure and approve again.');
        return;
      }
      const reapproval = action === 'approve' ? reapprovalFrom(error) : null;
      if (reapproval) {
        // D2: an approval of an earlier version never covers this update.
        setReapprovals((current) => ({ ...current, [requestId]: reapproval }));
        addToast(
          'error',
          reapproval.diff_changed
            ? 'The update changed since you reviewed it. Review the new changes and confirm again.'
            : 'This update needs explicit re-approval: review the changes and confirm.',
        );
        return;
      }
      addToast('error', `${action === 'approve' ? 'Approval' : 'Cancellation'} failed: ${errorMessage(error)}`);
    }
  }

  if (requests.isLoading) return <p className="p-4 text-sm text-text-tertiary">Loading install requests…</p>;
  if (requests.error) return <p role="alert" className="p-4 text-sm text-danger">Failed to load install requests: {errorMessage(requests.error)}</p>;
  const rows = requests.data?.items || [];
  return (
    <section aria-labelledby="project-marketplace-heading" className="space-y-4">
      <div>
        <h2 id="project-marketplace-heading" className="text-lg font-semibold text-text-primary">Marketplace approvals</h2>
        <p className="mt-1 text-sm text-text-secondary">Exact package hashes approved for this project. Approval permits instruction retrieval only; devices still control local writes and executable activation.</p>
      </div>
      <RevocationIncompleteNotice error={revocations.error} hasPrevious={!!revocations.data} />
      {rows.length === 0 ? (
        <div className="rounded-lg border border-border bg-bg-sunken p-6 text-center">
          <p className="text-sm font-medium text-text-primary">No install requests</p>
          <p className="mt-1 text-xs text-text-tertiary">Browse Marketplace and request a package for this project.</p>
        </div>
      ) : (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
          {rows.map((row) => {
            // Install requests carry only the version id, so package-wide
            // takedowns surface through the server's own status changes.
            const takedown = versionState(revocations.data, '', row.version_id);
            return (
            <li key={row.id} className="flex flex-col gap-3 bg-bg-elevated p-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm text-text-primary">{row.version_id}</span>
                  <Badge label={row.status} variant={requestVariant(row.status)} tint />
                  <ModerationStateBadge state={takedown} />
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
                  {canApprove && takedown !== 'active' && (
                    <p className="text-xs text-warning">This version is {takedown}; it can no longer be approved.</p>
                  )}
                  {canApprove && takedown === 'active' && (
                    <ApprovalRow
                      row={row}
                      isRequester={row.requested_by_user_id === me.data?.user_id}
                      pending={approve.isPending}
                      challengePending={challenge.isPending}
                      onChallenge={() => void sendChallenge(row.id)}
                      reapproval={reapprovals[row.id] || null}
                      resetToken={resetTokens[row.id] || 0}
                      onApprove={(approvedCapabilities, confirmationCode, binding, reloadDisclosure) =>
                        void decide(row.id, 'approve', approvedCapabilities, confirmationCode, binding, reloadDisclosure)}
                    />
                  )}
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
