import { useState } from 'react';
import type { MarketplaceReport, ModerationAction, ModerationTargetKind, QuarantineFinding, ReportReason, ScanFindingKey } from '../api/client';
import { Badge } from '../components/Badge';
import EmptyState from '../components/EmptyState';
import RelativeDate from '../components/RelativeDate';
import { Button, Dialog, DialogFooter, DialogHeader, Select, Textarea } from '../components/ui';
import {
  useDismissMarketplaceReport,
  useMarketplacePackageInfo,
  useMarketplaceReports,
  useModerateMarketplace,
  useRevocationIndex,
} from '../hooks/useMarketplace';
import { useMe } from '../hooks/useMe';
import MarketplaceNav from './MarketplaceNav';
import { useToast } from '../hooks/useToast';
import { useMyOrgs } from '../transfers/useTransfers';
import { envelopeOf, explainModerationError, type Explanation } from './marketplaceErrors';
import {
  REPORT_REASON_LABELS,
  canModerate,
  isPlatformAdmin,
  isRevocationFeedIncomplete,
  packageState,
  versionState,
  type ModerationState,
  type RevocationIndex,
} from './moderationState';
import { ExplanationAlert, ModerationStateBadge, RevocationIncompleteNotice } from './parts';

const MAX_NOTE = 500;

const STATUS_OPTIONS = [
  { value: 'open', label: 'Open reports' },
  { value: 'actioned', label: 'Actioned' },
  { value: 'dismissed', label: 'Dismissed' },
  { value: 'all', label: 'All reports' },
];

function statusVariant(status: string): 'warning' | 'success' | 'default' {
  if (status === 'open') return 'warning';
  if (status === 'actioned') return 'success';
  return 'default';
}

function reasonLabel(reason: string): string {
  return REPORT_REASON_LABELS[reason as ReportReason] ?? reason;
}

const ACTION_COPY: Record<ModerationAction, { verb: string; impact: string }> = {
  yank: {
    verb: 'Yank',
    impact:
      'Hides it from search, preview, download, feeds and new install requests. Existing grants stay recorded. Open reports on it are marked actioned. It can be restored later.',
  },
  block: {
    verb: 'Block',
    impact:
      'Hides it everywhere and revokes every install request, consent receipt and organization assignment that grants it. Open reports on it are marked actioned. Restoring later does NOT re-grant anything.',
  },
  restore: {
    verb: 'Restore',
    impact:
      'Makes it deliverable again. Grants revoked by a block stay revoked; people must request it again. Only a platform admin can undo a platform admin takedown.',
  },
};

function targetState(index: RevocationIndex | undefined, report: MarketplaceReport, kind: ModerationTargetKind): ModerationState {
  return kind === 'versions' ? versionState(index, report.package_id, report.version_id) : packageState(index, report.package_id);
}

function ModerationPanel({ report, index, feedIncomplete = false }: { report: MarketplaceReport; index: RevocationIndex | undefined; feedIncomplete?: boolean }) {
  const moderate = useModerateMarketplace();
  const { addToast } = useToast();
  const [kind, setKind] = useState<ModerationTargetKind>(report.version_id ? 'versions' : 'packages');
  const current = targetState(index, report, kind);
  const [action, setAction] = useState<ModerationAction>('yank');
  const [reason, setReason] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [problem, setProblem] = useState<Explanation | null>(null);
  // A quarantine restore that the server answered with 409 findings_required:
  // the critical findings the moderator must clear, BOUND to the exact
  // request that raised them (target kind, target id, action=restore).
  // Changing the target or the action discards it, and "Restore and clear"
  // always re-submits that exact tuple - never whatever is selected now.
  const [pending, setPending] = useState<
    { kind: ModerationTargetKind; targetId: string; findings: QuarantineFinding[] } | null
  >(null);
  const targetId = kind === 'versions' ? report.version_id! : report.package_id;
  const copy = ACTION_COPY[action];
  const pendingFindings = pending && pending.kind === kind && pending.targetId === targetId && action === 'restore'
    ? pending.findings
    : null;

  function chooseKind(next: ModerationTargetKind) {
    setPending(null);
    setConfirming(false);
    setKind(next);
  }

  function chooseAction(next: ModerationAction) {
    setPending(null);
    setConfirming(false);
    setAction(next);
  }

  async function run(request?: { kind: ModerationTargetKind; targetId: string; findings: ScanFindingKey[] }) {
    setProblem(null);
    const submitted = request
      ? { kind: request.kind, targetId: request.targetId, action: 'restore' as ModerationAction, findings: request.findings }
      : { kind, targetId, action, findings: undefined };
    try {
      const result = await moderate.mutateAsync({ ...submitted, reason: reason.trim() });
      setPending(null);
      const grants = Object.values(result.grants_revoked || {}).reduce((sum, value) => sum + (Number(value) || 0), 0);
      addToast(
        'success',
        `${copy.verb} applied (${result.previous_state} → ${result.state}). ${result.reports_resolved} report(s) resolved${grants ? `, ${grants} grant(s) revoked` : ''}.`,
      );
      setReason('');
    } catch (error) {
      setProblem(explainModerationError(error));
      const env = envelopeOf(error);
      const listed = env.code === 'findings_required' ? env.details.findings : null;
      setPending(
        submitted.action === 'restore' && Array.isArray(listed)
          ? { kind: submitted.kind, targetId: submitted.targetId, findings: listed as QuarantineFinding[] }
          : null,
      );
    } finally {
      setConfirming(false);
    }
  }

  return (
    <section aria-labelledby={`moderate-${report.id}`} className="rounded-lg border border-border bg-bg-sunken p-4">
      <h3 id={`moderate-${report.id}`} className="text-sm font-semibold text-text-primary">Take down or restore</h3>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Select
          title="Target"
          value={kind}
          onValueChange={(value) => chooseKind(value as ModerationTargetKind)}
          options={[
            ...(report.version_id ? [{ value: 'versions', label: `This version (${report.version_id})` }] : []),
            { value: 'packages', label: 'Whole package (every version)' },
          ]}
        />
        <Select
          title="Action"
          value={action}
          onValueChange={(value) => chooseAction(value as ModerationAction)}
          options={[
            { value: 'yank', label: 'Yank: withdraw from delivery' },
            { value: 'block', label: 'Block: withdraw and revoke grants' },
            { value: 'restore', label: 'Restore' },
          ]}
        />
      </div>
      <p className="mt-2 flex items-center gap-2 text-xs text-text-tertiary">
        Current state:{' '}
        {current !== 'active'
          ? <ModerationStateBadge state={current} />
          : feedIncomplete
            ? <span>unknown (revocations feed incomplete)</span>
            : <span>active</span>}
      </p>
      <div className="mt-3">
        <Textarea
          title="Reason (required, recorded in the audit log and revocations feed)"
          value={reason}
          maxLength={MAX_NOTE}
          onChange={(event) => setReason(event.target.value.slice(0, MAX_NOTE))}
        />
      </div>
      {problem && <div className="mt-3"><ExplanationAlert explanation={problem} /></div>}
      {pending && pendingFindings && pendingFindings.length > 0 && (
        <section aria-label="Critical findings to clear" className="mt-3 rounded-lg border border-warning/40 p-3 text-xs">
          <ul className="space-y-1">
            {pendingFindings.map((finding) => (
              <li key={`${finding.rule_id}|${finding.path}|${finding.line_sha}`} className="break-all font-mono text-text-primary">
                {finding.rule_id} — {finding.path}:{finding.line}
              </li>
            ))}
          </ul>
          <div className="mt-2 flex justify-end">
            <Button
              size="sm"
              variant="secondary"
              disabled={!reason.trim()}
              loading={moderate.isPending}
              onClick={() => run({
                kind: pending.kind,
                targetId: pending.targetId,
                findings: pending.findings.map(({ rule_id, path, line_sha }) => ({ rule_id, path, line_sha })),
              })}
            >
              Restore and clear these {pendingFindings.length} findings
            </Button>
          </div>
        </section>
      )}
      <div className="mt-3 flex justify-end">
        <Button
          variant={action === 'restore' ? 'secondary' : 'danger'}
          disabled={!reason.trim()}
          onClick={() => setConfirming(true)}
        >
          {copy.verb}…
        </Button>
      </div>
      {confirming && (
        // Inline confirmation (same pattern as "Delete version" in the
        // package dialog) so we never stack two modal focus traps.
        <section
          role="alertdialog"
          aria-labelledby={`confirm-${report.id}`}
          className={`mt-3 rounded-xl border p-4 ${action === 'restore' ? 'border-border' : 'border-danger/40 bg-[rgba(240,64,96,0.06)]'}`}
        >
          <h4 id={`confirm-${report.id}`} className={`text-sm font-semibold ${action === 'restore' ? 'text-text-primary' : 'text-danger'}`}>
            {copy.verb} {kind === 'versions' ? 'this version' : 'the whole package'}?
          </h4>
          <p className="mt-1 text-xs leading-5 text-text-secondary">{copy.impact}</p>
          <dl className="mt-3 space-y-1 rounded-lg border border-border bg-bg-sunken p-3 text-xs">
            <div className="flex gap-2"><dt className="text-text-tertiary">Target</dt><dd className="break-all font-mono text-text-primary">{targetId}</dd></div>
            <div className="flex gap-2"><dt className="text-text-tertiary">Reason</dt><dd className="whitespace-pre-wrap break-words text-text-primary">{reason.trim()}</dd></div>
          </dl>
          <div className="mt-3 flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            <Button size="sm" variant={action === 'restore' ? 'primary' : 'danger'} loading={moderate.isPending} onClick={() => run()}>
              Confirm {copy.verb.toLowerCase()}
            </Button>
          </div>
        </section>
      )}
    </section>
  );
}

function DismissPanel({ report }: { report: MarketplaceReport }) {
  const dismiss = useDismissMarketplaceReport();
  const { addToast } = useToast();
  const [note, setNote] = useState('');
  const [problem, setProblem] = useState<Explanation | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);
    try {
      await dismiss.mutateAsync({ reportId: report.id, note: note.trim() });
      addToast('success', 'Report dismissed.');
      setNote('');
    } catch (error) {
      setProblem(explainModerationError(error));
    }
  }

  return (
    <form onSubmit={submit} className="rounded-lg border border-border bg-bg-sunken p-4" aria-label="Dismiss report">
      <h3 className="text-sm font-semibold text-text-primary">Dismiss without action</h3>
      <div className="mt-3">
        <Textarea
          title="Dismissal note (required)"
          value={note}
          maxLength={MAX_NOTE}
          onChange={(event) => setNote(event.target.value.slice(0, MAX_NOTE))}
        />
      </div>
      {problem && <div className="mt-3"><ExplanationAlert explanation={problem} /></div>}
      <div className="mt-3 flex justify-end">
        <Button type="submit" variant="secondary" disabled={!note.trim()} loading={dismiss.isPending}>Dismiss report</Button>
      </div>
    </form>
  );
}

function ReportDetail({ report, index, feedIncomplete = false, onClose }: { report: MarketplaceReport; index: RevocationIndex | undefined; feedIncomplete?: boolean; onClose: () => void }) {
  const info = useMarketplacePackageInfo(report.package_id);
  const titleId = `report-detail-${report.id}`;
  return (
    <Dialog open onClose={onClose} titleId={titleId} className="max-w-3xl max-h-[90vh] overflow-y-auto">
      <p className="text-2xs font-semibold uppercase tracking-[0.16em] text-brand">Marketplace report</p>
      <DialogHeader titleId={titleId}>{reasonLabel(report.reason)}</DialogHeader>
      <div className="-mt-2 mb-4 flex flex-wrap gap-2">
        <Badge label={report.status} variant={statusVariant(report.status)} tint />
        <ModerationStateBadge state={versionState(index, report.package_id, report.version_id)} />
        {report.scope_kind && <Badge label={`${report.scope_kind} scope`} tint />}
      </div>
      <dl className="grid gap-x-6 gap-y-3 rounded-xl border border-border bg-bg-sunken p-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-text-tertiary">Package</dt>
          <dd className="mt-1 text-text-primary">
            {info.data ? (
              <span>{info.data.display_name} <span className="font-mono text-xs text-text-tertiary">{info.data.collection} / {info.data.namespace}</span></span>
            ) : info.isLoading ? (
              <span className="text-text-tertiary">Loading…</span>
            ) : (
              <span className="text-text-tertiary">Not currently listed for you (yanked, blocked or hidden)</span>
            )}
            <span className="block break-all font-mono text-xs text-text-tertiary">{report.package_id}</span>
          </dd>
        </div>
        <div>
          <dt className="text-xs text-text-tertiary">Version</dt>
          <dd className="mt-1 break-all font-mono text-text-primary">{report.version_id || 'Whole package'}</dd>
        </div>
        <div><dt className="text-xs text-text-tertiary">Filed</dt><dd className="mt-1 text-text-primary"><RelativeDate iso={report.created_at} /></dd></div>
        <div><dt className="text-xs text-text-tertiary">Reporter</dt><dd className="mt-1 break-all font-mono text-text-primary">{report.reporter_user_id || 'hidden'}</dd></div>
        {report.scope_org_id && <div><dt className="text-xs text-text-tertiary">Organization</dt><dd className="mt-1 break-all font-mono text-text-primary">{report.scope_org_id}</dd></div>}
      </dl>
      <section className="mt-4" aria-label="Reporter details">
        <h3 className="text-sm font-semibold text-text-primary">Reporter's details</h3>
        <p className="mt-1 text-2xs text-text-tertiary">Untrusted text from the reporter, shown as plain text.</p>
        <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-bg-sunken p-3 text-xs text-text-secondary">
          {report.details || 'No details provided.'}
        </pre>
      </section>
      {report.status !== 'open' && report.resolution_note && (
        <section className="mt-4 rounded-lg border border-border p-3 text-sm">
          <h3 className="text-xs uppercase tracking-wider text-text-tertiary">Resolution</h3>
          <p className="mt-1 whitespace-pre-wrap break-words text-text-secondary">{report.resolution_note}</p>
          {report.resolved_at && <p className="mt-1 text-xs text-text-tertiary">Resolved <RelativeDate iso={report.resolved_at} /></p>}
        </section>
      )}
      <div className="mt-5 grid gap-4">
        {report.status === 'open' && <DismissPanel report={report} />}
        <ModerationPanel report={report} index={index} feedIncomplete={feedIncomplete} />
      </div>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>Close</Button>
      </DialogFooter>
    </Dialog>
  );
}

export default function ModerationPage() {
  const me = useMe();
  const orgs = useMyOrgs();
  const allowed = canModerate(me.data, orgs.data);
  const [status, setStatus] = useState('open');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const reports = useMarketplaceReports(status, allowed);
  const revocations = useRevocationIndex(allowed);
  const items = reports.data?.pages.flatMap((page) => page.items) ?? [];
  const selected = items.find((item) => item.id === selectedId) ?? null;

  if (me.isLoading || orgs.isLoading) {
    return <div className="mx-auto w-full max-w-6xl px-4 py-7 text-sm text-text-tertiary sm:px-6">Loading…</div>;
  }

  if (!allowed) {
    return (
      <div className="mx-auto w-full max-w-6xl px-4 py-7 sm:px-6">
        <EmptyState
          title="Moderation is limited to admins"
          description="The marketplace moderation queue is available to platform admins and to owners and admins of an organization."
        />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-7 sm:px-6">
      <MarketplaceNav />
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-text-primary">Moderation queue</h1>
          <p className="mt-1 max-w-2xl text-sm text-text-secondary">
            {isPlatformAdmin(me.data)
              ? 'Reports on public and personal listings, plus any organization you administer.'
              : 'Reports on listings in the organizations you own or administer.'}{' '}
            Every action is audited and published to the revocations feed.
          </p>
        </div>
        <div className="w-56">
          <Select aria-label="Filter reports by status" value={status} onValueChange={(value) => { setStatus(value); setSelectedId(null); }} options={STATUS_OPTIONS} />
        </div>
      </header>

      <RevocationIncompleteNotice error={revocations.error} hasPrevious={!!revocations.data} className="mt-4" />

      {reports.isLoading && <p className="mt-6 text-sm text-text-tertiary">Loading reports…</p>}
      {reports.error && (
        <div className="mt-6"><ExplanationAlert explanation={explainModerationError(reports.error)} /></div>
      )}
      {reports.data && items.length === 0 && (
        <div className="mt-6">
          <EmptyState title="Nothing to review" description="No reports match this filter for the listings you moderate." />
        </div>
      )}
      {items.length > 0 && (
        <ul className="mt-6 divide-y divide-border overflow-hidden rounded-xl border border-border bg-bg-elevated">
          {items.map((report) => (
            <li key={report.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-text-primary">{reasonLabel(report.reason)}</span>
                  <Badge label={report.status} variant={statusVariant(report.status)} tint />
                  <ModerationStateBadge state={versionState(revocations.data, report.package_id, report.version_id)} />
                </div>
                <p className="mt-1 truncate font-mono text-xs text-text-tertiary">
                  {report.package_id}{report.version_id ? ` · ${report.version_id}` : ' · whole package'}
                </p>
                {report.details && <p className="mt-1 line-clamp-1 text-xs text-text-secondary">{report.details}</p>}
              </div>
              <span className="text-xs text-text-tertiary"><RelativeDate iso={report.created_at} /></span>
              <Button size="sm" variant="secondary" onClick={() => setSelectedId(report.id)} aria-label={`Review report ${report.id}`}>
                Review
              </Button>
            </li>
          ))}
        </ul>
      )}
      {reports.hasNextPage && (
        <div className="mt-5 flex justify-center">
          <Button variant="secondary" onClick={() => void reports.fetchNextPage()} loading={reports.isFetchingNextPage}>Load more</Button>
        </div>
      )}
      {selected && <ReportDetail report={selected} index={revocations.data} feedIncomplete={isRevocationFeedIncomplete(revocations.error)} onClose={() => setSelectedId(null)} />}
    </div>
  );
}
