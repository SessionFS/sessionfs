import { Link, useSearchParams } from 'react-router-dom';
import type { ApprovalQueueItem } from '../api/client';
import { Badge } from '../components/Badge';
import RelativeDate from '../components/RelativeDate';
import { Card, PageContainer, PageHeader } from '../components/ui';
import { useMyApprovals } from '../hooks/useApprovals';
import { errorMessage } from '../utils/errorMessage';
import { ApprovalDrawer } from './ApprovalDrawer';
import { stateLabel, stateVariant, TARGET_LABEL, targetHref } from './format';

/** Approvals I can decide now. Deep link: /approvals?request=<id>. */
export default function ApprovalsPage() {
  const [params, setParams] = useSearchParams();
  const openId = params.get('request');
  const { data, isLoading, error } = useMyApprovals();

  function openRequest(id: string | null) {
    const next = new URLSearchParams(params);
    if (id) next.set('request', id);
    else next.delete('request');
    setParams(next, { replace: !id });
  }

  const items = data?.requests ?? [];
  return (
    <PageContainer width="narrow" className="space-y-5">
      <PageHeader
        title="Approvals"
        description="Changes and knowledge entries waiting for your sign-off under your organization's approval policies."
      />
      {isLoading && <p className="text-sm text-text-tertiary">Loading approvals…</p>}
      {error && (
        <p role="alert" className="text-sm text-danger">
          Failed to load approvals: {errorMessage(error)}
        </p>
      )}
      {!isLoading && !error && items.length === 0 && (
        <p className="text-sm text-text-tertiary">Nothing is waiting for your approval.</p>
      )}
      {items.length > 0 && (
        <ul aria-label="Approval requests" className="space-y-2">
          {items.map((r) => (
            <li key={r.id}>
              <ApprovalRow item={r} onOpen={openRequest} />
            </li>
          ))}
        </ul>
      )}
      <ApprovalDrawer requestId={openId} onClose={() => openRequest(null)} />
    </PageContainer>
  );
}

function ApprovalRow({ item: r, onOpen }: { item: ApprovalQueueItem; onOpen: (id: string) => void }) {
  const href = targetHref(r.project_id, r.target_kind, r.target_id);
  return (
    <Card className="p-4" data-testid={`approval-${r.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <button
            type="button"
            onClick={() => onOpen(r.id)}
            className="block max-w-full truncate rounded text-left font-medium text-text-primary outline-none hover:text-brand focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
          >
            {r.target_title || r.target_id}
          </button>
          <p className="mt-0.5 text-xs text-text-tertiary">
            {r.policy_name ?? 'Policy'} · {TARGET_LABEL[r.target_kind] ?? r.target_kind}
            {href && (
              <>
                {' · '}
                <Link to={href} className="text-brand hover:underline">
                  Open target
                </Link>
              </>
            )}
          </p>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-text-tertiary">
            {r.requested_at && (
              <span>
                Requested <RelativeDate iso={r.requested_at} />
              </span>
            )}
            {r.expires_at && (
              <span>
                Expires <RelativeDate iso={r.expires_at} />
              </span>
            )}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <Badge variant={stateVariant(r.state)} tint label={stateLabel(r.state)} size="sm" />
          <span className="text-xs text-text-secondary">
            {r.approved_count} of {r.required_count} approved
          </span>
        </div>
      </div>
    </Card>
  );
}
