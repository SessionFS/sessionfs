import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { ApprovalRequestDetail } from '../api/client';
import { Badge } from '../components/Badge';
import RelativeDate from '../components/RelativeDate';
import { Button, Drawer, MonoText, Textarea } from '../components/ui';
import { useApprovalRequest, useDecideApproval } from '../hooks/useApprovals';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { stateLabel, stateVariant, summarizeRequirements, TARGET_LABEL, targetHref } from './format';
import { Term } from '../help/Term';

/** Drawer for one approval request, opened by `?request=<id>`. */
export function ApprovalDrawer({ requestId, onClose }: { requestId: string | null; onClose: () => void }) {
  const { data, isLoading, error } = useApprovalRequest(requestId);
  return (
    <Drawer open={!!requestId} onClose={onClose} titleId="approval-drawer-title">
      {isLoading && <p className="text-sm text-text-tertiary">Loading request…</p>}
      {error && (
        <div>
          <h2 id="approval-drawer-title" className="text-lg font-semibold text-text-primary">
            Approval request
          </h2>
          <p role="alert" className="mt-3 text-sm text-danger">
            {errorMessage(error, 'This request is not available.')}
          </p>
        </div>
      )}
      {data && <RequestBody key={data.id} request={data} />}
    </Drawer>
  );
}

function RequestBody({ request: r }: { request: ApprovalRequestDetail }) {
  const { addToast } = useToast();
  const decide = useDecideApproval(r.id);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState<string | undefined>();
  const [actionError, setActionError] = useState<string | null>(null);
  const href = targetHref(r.project_id, r.target_kind, r.target_id);
  const canDecide = r.can_decide && r.state === 'pending';

  function submit(decision: 'approve' | 'reject') {
    setActionError(null);
    if (!reason.trim()) {
      setReasonError('Give a reason. It is kept in the audit trail.');
      return;
    }
    setReasonError(undefined);
    decide.mutate(
      { decision, reason: reason.trim() },
      {
        onSuccess: () => {
          addToast('success', decision === 'approve' ? 'Approved' : 'Rejected');
          setReason('');
        },
        onError: (e) => setActionError(errorMessage(e)),
      },
    );
  }

  return (
    <div className="space-y-5">
      <div className="pr-8">
        <h2 id="approval-drawer-title" className="text-lg font-semibold text-text-primary [overflow-wrap:anywhere]">
          {r.policy.name ?? 'Approval request'}
        </h2>
        <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-text-secondary">
          <Badge variant={stateVariant(r.state)} tint label={stateLabel(r.state)} size="sm" />
          <span>
            {r.approved_count} of {r.required_count} approved
          </span>
          {r.policy.version != null && <span className="text-text-tertiary">Policy v{r.policy.version}</span>}
          {!r.policy.blocking && (
            <Term id="approval-advisory">
              <Badge variant="default" tint label="Advisory" size="sm" />
            </Term>
          )}
        </p>
      </div>

      <dl className="space-y-3 text-sm">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-text-tertiary">Target</dt>
          <dd className="text-text-primary">
            {TARGET_LABEL[r.target_kind] ?? r.target_kind}:{' '}
            {href ? (
              <Link to={href} className="text-brand hover:underline">
                {r.target_id}
              </Link>
            ) : (
              r.target_id
            )}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-text-tertiary">
            {r.target_kind === 'merge' ? 'Commit' : 'Content fingerprint'}
          </dt>
          <dd>
            <MonoText value={r.target_fingerprint} copyable />
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-text-tertiary">Needs</dt>
          <dd className="text-text-primary">{summarizeRequirements(r.policy.requirements)}</dd>
        </div>
      </dl>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-text-secondary">
        {r.requested_at && (
          <span>
            Requested <RelativeDate iso={r.requested_at} />
          </span>
        )}
        {r.expires_at && r.state === 'pending' && (
          <span>
            Expires <RelativeDate iso={r.expires_at} />
          </span>
        )}
        {r.decided_at && (
          <span>
            Decided <RelativeDate iso={r.decided_at} />
          </span>
        )}
      </div>

      <section aria-label="Decisions">
        <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-text-tertiary">Decisions</h3>
        {r.decisions.length === 0 ? (
          <p className="text-sm text-text-tertiary">No decisions yet.</p>
        ) : (
          <ul className="space-y-2">
            {r.decisions.map((d) => (
              <li key={d.id} className="rounded-lg border border-border bg-surface p-3 text-sm">
                <p className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-text-primary">{d.approver_name ?? 'Someone'}</span>
                  <Badge
                    variant={d.decision === 'approve' ? 'success' : 'danger'}
                    tint
                    label={d.decision === 'approve' ? 'Approved' : 'Rejected'}
                    size="sm"
                  />
                  {d.decided_at && (
                    <span className="text-xs text-text-tertiary">
                      <RelativeDate iso={d.decided_at} />
                    </span>
                  )}
                </p>
                {d.reason && <p className="mt-1 whitespace-pre-wrap text-text-secondary">{d.reason}</p>}
              </li>
            ))}
          </ul>
        )}
      </section>

      {canDecide && (
        <form
          aria-label="Decide"
          className="space-y-3"
          onSubmit={(e) => e.preventDefault()}
        >
          <Textarea
            title="Reason"
            value={reason}
            rows={3}
            maxLength={2000}
            error={reasonError}
            hint="Required. Shown in the approval record."
            onChange={(e) => {
              setReason(e.target.value);
              if (reasonError) setReasonError(undefined);
            }}
          />
          {actionError && (
            <p role="alert" className="text-sm text-danger">
              {actionError}
            </p>
          )}
          <div className="flex gap-2">
            <Button loading={decide.isPending} onClick={() => submit('approve')}>
              Approve
            </Button>
            <Button variant="danger" disabled={decide.isPending} onClick={() => submit('reject')}>
              Reject
            </Button>
          </div>
        </form>
      )}
      {!canDecide && r.state === 'pending' && (
        <p className="text-sm text-text-tertiary">You can read this request but cannot decide it.</p>
      )}
    </div>
  );
}
