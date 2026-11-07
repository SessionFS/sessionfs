import { useMemo, useState } from 'react';
import type { ApprovalPolicy } from '../api/client';
import { Alert, Badge, Button, Card, Checkbox, Dialog, DialogFooter, DialogHeader, TableShell } from '../components/ui';
import { useApprovalPolicies, useDisableApprovalPolicy } from '../hooks/useApprovals';
import { useProjects } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { useOrgMembers } from '../org/useOrgMembers';
import { errorMessage } from '../utils/errorMessage';
import { summarizeFilter, summarizeRequirements, TARGET_LABEL } from './format';
import { PolicyDialog } from './PolicyDialog';

/** Org → Governance: approval policies. Everyone reads; owners and admins manage. */
export default function PoliciesSection({ orgId }: { orgId: string }) {
  const [showRetired, setShowRetired] = useState(false);
  const { data, isLoading, error } = useApprovalPolicies(orgId, showRetired);
  const projects = useProjects();
  const members = useOrgMembers(orgId);
  const disable = useDisableApprovalPolicy(orgId);
  const { addToast } = useToast();
  const [editing, setEditing] = useState<ApprovalPolicy | 'new' | null>(null);
  const [disabling, setDisabling] = useState<ApprovalPolicy | null>(null);
  const [disableError, setDisableError] = useState<string | null>(null);

  const orgProjects = useMemo(
    () => (projects.data ?? []).filter((p) => p.org_id === orgId).map((p) => ({ id: p.id, name: p.name })),
    [projects.data, orgId],
  );
  const pickerMembers = useMemo(
    () =>
      (members.data?.members ?? []).map((m) => ({
        user_id: m.user_id,
        label: m.display_name ? `${m.display_name} (${m.email})` : m.email,
      })),
    [members.data],
  );
  const memberName = (id: string) => {
    const m = members.data?.members.find((x) => x.user_id === id);
    return m ? m.display_name || m.email : id;
  };
  const projectName = (id: string) => orgProjects.find((p) => p.id === id)?.name ?? id;

  const canManage = !!data?.can_manage;
  const planOk = !!data?.plan_includes_approvals;
  const policies = data?.policies ?? [];

  function confirmDisable() {
    if (!disabling) return;
    setDisableError(null);
    disable.mutate(disabling.id, {
      onSuccess: () => {
        addToast('success', 'Policy disabled');
        setDisabling(null);
      },
      onError: (e) => setDisableError(errorMessage(e)),
    });
  }

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="approval-policies-heading" className="space-y-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="approval-policies-heading" className="text-base font-semibold text-text-primary">
              Approval policies
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-text-tertiary">
              Rules that hold a merge or a knowledge promotion until the right people sign off. Each decision is
              recorded with the policy version, the approver and the reason.
            </p>
          </div>
          {canManage && planOk && <Button onClick={() => setEditing('new')}>New policy</Button>}
        </div>

        {data && !planOk && (
          <Alert variant="info" title="Approval policies are an Enterprise feature">
            {canManage
              ? 'Upgrade this organization to Enterprise to create or edit policies. Existing policies can still be disabled.'
              : 'Ask an organization owner or admin about upgrading to Enterprise to use approval policies.'}
          </Alert>
        )}
        {data && planOk && !canManage && (
          <p className="text-sm text-text-tertiary">Only organization owners and admins can change policies.</p>
        )}

        {isLoading && <p className="text-sm text-text-tertiary">Loading policies…</p>}
        {error && (
          <p role="alert" className="text-sm text-danger">
            Failed to load policies: {errorMessage(error)}
          </p>
        )}
        {canManage && (
          <Checkbox
            label="Show retired versions"
            checked={showRetired}
            onChange={(e) => setShowRetired(e.target.checked)}
          />
        )}

        {data && policies.length === 0 && (
          <p className="text-sm text-text-tertiary">No approval policies yet.</p>
        )}
        {policies.length > 0 && (
          <TableShell label="Policy list" minWidth={720} bordered>
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-text-tertiary">
                <th scope="col" className="px-3 py-2 font-medium">Policy</th>
                <th scope="col" className="px-3 py-2 font-medium">Applies to</th>
                <th scope="col" className="px-3 py-2 font-medium">Needs</th>
                <th scope="col" className="px-3 py-2 font-medium">Mode</th>
                <th scope="col" className="px-3 py-2 font-medium">Version</th>
                {canManage && <th scope="col" className="px-3 py-2 font-medium"><span className="sr-only">Actions</span></th>}
              </tr>
            </thead>
            <tbody>
              {policies.map((p) => {
                const when = summarizeFilter(p.filter, projectName);
                return (
                  <tr key={p.id} className="border-t border-border align-top" data-testid={`policy-${p.id}`}>
                    <td className="px-3 py-2">
                      <span className="font-medium text-text-primary">{p.name}</span>
                      {!p.active && (
                        <span className="ml-2">
                          <Badge variant="default" tint label="Retired" size="sm" />
                        </span>
                      )}
                      {when && <span className="mt-0.5 block text-xs text-text-tertiary">{when}</span>}
                    </td>
                    <td className="px-3 py-2 text-text-secondary">{TARGET_LABEL[p.target_kind] ?? p.target_kind}</td>
                    <td className="px-3 py-2 text-text-secondary">{summarizeRequirements(p.requirements, memberName)}</td>
                    <td className="px-3 py-2">
                      <Badge
                        variant={p.blocking ? 'warning' : 'default'}
                        tint
                        label={p.blocking ? 'Blocking' : 'Advisory'}
                        size="sm"
                      />
                    </td>
                    <td className="px-3 py-2 text-text-secondary">v{p.version}</td>
                    {canManage && (
                      <td className="px-3 py-2">
                        {p.active && (
                          <div className="flex justify-end gap-1">
                            {planOk && (
                              <Button size="sm" variant="ghost" onClick={() => setEditing(p)} aria-label={`Edit ${p.name}`}>
                                Edit
                              </Button>
                            )}
                            <Button
                              size="sm"
                              variant="ghost"
                              className="text-danger"
                              onClick={() => {
                                setDisableError(null);
                                setDisabling(p);
                              }}
                              aria-label={`Disable ${p.name}`}
                            >
                              Disable
                            </Button>
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </TableShell>
        )}
      </section>

      <PolicyDialog
        open={editing !== null}
        onClose={() => setEditing(null)}
        orgId={orgId}
        policy={editing && editing !== 'new' ? editing : undefined}
        projects={orgProjects}
        members={pickerMembers}
      />
      <Dialog open={!!disabling} onClose={() => setDisabling(null)} titleId="disable-policy-title">
        <DialogHeader titleId="disable-policy-title">Disable this policy?</DialogHeader>
        <p className="text-sm text-text-secondary">
          {disabling?.name} stops applying to new work. Requests already raised keep their record, and anything
          waiting on this policy is no longer held.
        </p>
        {disableError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {disableError}
          </p>
        )}
        <DialogFooter>
          <Button variant="secondary" onClick={() => setDisabling(null)}>
            Cancel
          </Button>
          <Button variant="danger" loading={disable.isPending} onClick={confirmDisable}>
            Disable
          </Button>
        </DialogFooter>
      </Dialog>
    </Card>
  );
}
