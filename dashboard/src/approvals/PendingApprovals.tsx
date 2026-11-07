import { Link } from 'react-router-dom';
import type { TicketApproval } from '../api/client';
import { Badge } from '../components/Badge';
import { approvalRequestHref, stateLabel, stateVariant } from './format';

/** Blocking approvals that still hold the change. */
export function awaitingApprovals(approvals: TicketApproval[] | undefined): TicketApproval[] {
  return (approvals ?? []).filter((a) => a.blocking && a.state !== 'approved');
}

/** Server reasons that restate an approval line the seal already shows. */
export function withoutApprovalReasons(reasons: string[], approvals: TicketApproval[] | undefined): string[] {
  const names = awaitingApprovals(approvals).map((a) => a.policy_name);
  if (names.length === 0) return reasons;
  return reasons.filter((r) => !names.some((n) => n && r.includes(n)));
}

const stateText = (a: TicketApproval): string => {
  if (a.state === 'not_requested') return 'not requested yet';
  if (a.state === 'limit') return 'request limit reached';
  return `${a.approved_count} of ${a.required_count}`;
};

/** "Awaiting approval: <policy> (n of m)", each linking to its request. */
export function AwaitingApprovalLines({ approvals }: { approvals: TicketApproval[] | undefined }) {
  const pending = awaitingApprovals(approvals);
  if (pending.length === 0) return null;
  return (
    <ul className="mt-1.5 space-y-0.5 text-text-tertiary" aria-label="Awaiting approval">
      {pending.map((a) => (
        <li key={a.policy_id}>
          Awaiting approval:{' '}
          {a.request_id ? (
            <Link to={approvalRequestHref(a.request_id)} className="text-brand hover:underline">
              {a.policy_name}
            </Link>
          ) : (
            a.policy_name
          )}{' '}
          ({stateText(a)})
        </li>
      ))}
    </ul>
  );
}

/** Quiet advisory notes. */
export function ApprovalWarnings({ warnings }: { warnings: string[] | undefined }) {
  if (!warnings || warnings.length === 0) return null;
  return (
    <ul className="mt-1.5 space-y-0.5 text-xs text-text-tertiary" aria-label="Approval notes">
      {warnings.map((w) => (
        <li key={w}>{w}</li>
      ))}
    </ul>
  );
}

/** Every matching policy on a ticket, for the Story. */
export function ApprovalsPanel({ approvals, warnings }: { approvals?: TicketApproval[]; warnings?: string[] }) {
  if ((!approvals || approvals.length === 0) && (!warnings || warnings.length === 0)) return null;
  return (
    <section aria-label="Approvals" className="space-y-2">
      <h3 className="text-xs font-medium uppercase tracking-wide text-text-tertiary">Approvals</h3>
      {approvals && approvals.length > 0 && (
        <ul className="space-y-1.5 text-sm">
          {approvals.map((a) => (
            <li key={a.policy_id} className="flex flex-wrap items-center gap-2">
              <span className="text-text-primary">{a.policy_name}</span>
              <Badge variant={stateVariant(a.state)} tint label={a.state === 'not_requested' ? 'Not requested' : stateLabel(a.state)} size="sm" />
              {!a.blocking && <Badge variant="default" tint label="Advisory" size="sm" />}
              <span className="text-xs text-text-tertiary">
                {a.approved_count} of {a.required_count}
              </span>
              {a.request_id && (
                <Link to={approvalRequestHref(a.request_id)} className="text-xs text-brand hover:underline">
                  View request
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
      <ApprovalWarnings warnings={warnings} />
    </section>
  );
}
