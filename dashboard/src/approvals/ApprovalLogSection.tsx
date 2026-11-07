import { useMemo, useState } from 'react';
import type { ApprovalLogFilters } from '../api/client';
import { Alert, Badge, Button, Card, Input, Select, TableShell } from '../components/ui';
import { useAuth } from '../auth/AuthContext';
import { useApprovalLog, useApprovalPolicies } from '../hooks/useApprovals';
import { useProjects } from '../hooks/useProjects';
import { useOrgMembers } from '../org/useOrgMembers';
import { errorMessage } from '../utils/errorMessage';
import { stateLabel, stateVariant, TARGET_LABEL } from './format';

const ALL = '';

/** `until` is exclusive on the server, so a picked day covers that whole day. */
function dayAfter(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString();
}

function when(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Org → Governance → Approval log: every decision, newest first. Owners and admins. */
export default function ApprovalLogSection({ orgId }: { orgId: string }) {
  const { auth } = useAuth();
  const [project, setProject] = useState(ALL);
  const [policy, setPolicy] = useState(ALL);
  const [decision, setDecision] = useState(ALL);
  const [approver, setApprover] = useState(ALL);
  const [since, setSince] = useState('');
  const [until, setUntil] = useState('');
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [download, setDownload] = useState<{ url: string; truncated: boolean } | null>(null);

  const filters = useMemo<ApprovalLogFilters>(
    () => ({
      project_id: project || undefined,
      policy_id: policy || undefined,
      decision: (decision || undefined) as ApprovalLogFilters['decision'],
      approver: approver || undefined,
      since: since ? `${since}T00:00:00Z` : undefined,
      until: until ? dayAfter(until) : undefined,
    }),
    [project, policy, decision, approver, since, until],
  );

  const log = useApprovalLog(orgId, filters);
  const policies = useApprovalPolicies(orgId, true);
  const projects = useProjects();
  const members = useOrgMembers(orgId);

  const orgProjects = (projects.data ?? []).filter((p) => p.org_id === orgId);
  const projectName = (id: string) => orgProjects.find((p) => p.id === id)?.name ?? id;
  const rows = log.data?.pages.flatMap((p) => p.items) ?? [];

  // One option per policy lineage (the newest version's id and name).
  const policyOptions = useMemo(() => {
    const seen = new Map<string, { id: string; name: string }>();
    for (const p of policies.data?.policies ?? []) if (!seen.has(p.lineage_id)) seen.set(p.lineage_id, { id: p.id, name: p.name });
    return [...seen.values()].map((p) => ({ value: p.id, label: p.name }));
  }, [policies.data]);

  async function exportCsv() {
    if (!auth) return;
    setExporting(true);
    setExportError(null);
    try {
      const { blob, truncated } = await auth.client.exportApprovalLogCsv(orgId, filters);
      if (download) URL.revokeObjectURL(download.url);
      setDownload({ url: URL.createObjectURL(blob), truncated });
    } catch (e) {
      setExportError(errorMessage(e));
    } finally {
      setExporting(false);
    }
  }

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="approval-log-heading" className="space-y-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="approval-log-heading" className="text-base font-semibold text-text-primary">
              Approval log
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-text-tertiary">
              Every approve and reject decision in this organization, newest first, with the policy version that
              applied.
            </p>
          </div>
          <Button variant="secondary" onClick={exportCsv} disabled={exporting}>
            {exporting ? 'Preparing…' : 'Export CSV'}
          </Button>
        </div>

        {exportError && <Alert variant="danger">Could not export the log: {exportError}</Alert>}
        {download && (
          <Alert variant="success" title="Export ready">
            <a
              href={download.url}
              download={`approval-log-${orgId}.csv`}
              className="font-medium text-brand underline"
            >
              Download approval-log.csv
            </a>
            {download.truncated && ' The export stops at 10,000 rows; narrow the filters to get the rest.'}
          </Alert>
        )}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Select
            title="Project"
            value={project}
            onValueChange={setProject}
            options={[{ value: ALL, label: 'All projects' }, ...orgProjects.map((p) => ({ value: p.id, label: p.name }))]}
          />
          <Select
            title="Policy"
            value={policy}
            onValueChange={setPolicy}
            options={[{ value: ALL, label: 'All policies' }, ...policyOptions]}
          />
          <Select
            title="Decision"
            value={decision}
            onValueChange={setDecision}
            options={[
              { value: ALL, label: 'Approved and rejected' },
              { value: 'approve', label: 'Approved' },
              { value: 'reject', label: 'Rejected' },
            ]}
          />
          <Select
            title="Approver"
            value={approver}
            onValueChange={setApprover}
            options={[
              { value: ALL, label: 'Anyone' },
              ...(members.data?.members ?? []).map((m) => ({
                value: m.user_id,
                label: m.display_name || m.email,
              })),
            ]}
          />
          <Input title="From" type="date" value={since} onChange={(e) => setSince(e.target.value)} />
          <Input title="To" type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        </div>

        {log.isLoading && <p className="text-sm text-text-tertiary">Loading the log…</p>}
        {log.error && (
          <p role="alert" className="text-sm text-danger">
            Failed to load the approval log: {errorMessage(log.error)}
          </p>
        )}
        {log.data && rows.length === 0 && (
          <p className="text-sm text-text-tertiary">No decisions match these filters.</p>
        )}
        {rows.length > 0 && (
          <TableShell label="Approval decisions" minWidth={820} bordered>
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-text-tertiary">
                <th scope="col" className="px-3 py-2 font-medium">When</th>
                <th scope="col" className="px-3 py-2 font-medium">Decision</th>
                <th scope="col" className="px-3 py-2 font-medium">Approver</th>
                <th scope="col" className="px-3 py-2 font-medium">Policy</th>
                <th scope="col" className="px-3 py-2 font-medium">Project</th>
                <th scope="col" className="px-3 py-2 font-medium">Request</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.decision_id} className="border-t border-border align-top" data-testid={`log-${r.decision_id}`}>
                  <td className="px-3 py-2 text-text-secondary">{when(r.decided_at)}</td>
                  <td className="px-3 py-2">
                    <Badge
                      variant={r.decision === 'approve' ? 'success' : 'danger'}
                      tint
                      label={r.decision === 'approve' ? 'Approved' : 'Rejected'}
                      size="sm"
                    />
                    {r.reason && <span className="mt-0.5 block text-xs text-text-tertiary">{r.reason}</span>}
                  </td>
                  <td className="px-3 py-2 text-text-secondary">{r.approver_name ?? r.approver_user_id}</td>
                  <td className="px-3 py-2 text-text-secondary">
                    {r.policy_name ?? r.policy_id}
                    {r.policy_version != null && <span className="text-text-tertiary"> v{r.policy_version}</span>}
                    <span className="mt-0.5 block text-xs text-text-tertiary">
                      {TARGET_LABEL[r.target_kind] ?? r.target_kind}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-text-secondary">{projectName(r.project_id)}</td>
                  <td className="px-3 py-2">
                    <Badge variant={stateVariant(r.request_state)} tint label={stateLabel(r.request_state)} size="sm" />
                  </td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        )}
        {log.hasNextPage && (
          <Button variant="secondary" onClick={() => log.fetchNextPage()} disabled={log.isFetchingNextPage}>
            {log.isFetchingNextPage ? 'Loading…' : 'Load more'}
          </Button>
        )}
      </section>
    </Card>
  );
}
