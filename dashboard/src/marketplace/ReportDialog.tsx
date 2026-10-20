import { useState } from 'react';
import { REPORT_REASONS, type MarketplacePackage, type ReportReason } from '../api/client';
import { Button, Dialog, DialogFooter, DialogHeader, Select, Textarea } from '../components/ui';
import { useReportMarketplacePackage } from '../hooks/useMarketplace';
import { useToast } from '../hooks/useToast';
import { envelopeOf, explainReportError, type Explanation } from './marketplaceErrors';
import { REPORT_REASON_LABELS } from './moderationState';
import { ExplanationAlert } from './parts';

const MAX_REPORT_DETAILS = 2000;

export default function ReportDialog({ item, onClose }: { item: MarketplacePackage; onClose: () => void }) {
  const report = useReportMarketplacePackage();
  const { addToast } = useToast();
  const [reason, setReason] = useState<ReportReason | ''>('');
  const [details, setDetails] = useState('');
  const [thisVersion, setThisVersion] = useState(true);
  const [problem, setProblem] = useState<{ explanation: Explanation; tone: 'danger' | 'warning' } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!reason) return;
    setProblem(null);
    try {
      await report.mutateAsync({
        packageId: item.package_id,
        reason,
        details: details.trim() || undefined,
        ...(thisVersion ? { version_id: item.version_id } : {}),
      });
      addToast('success', 'Report filed. Moderators for this listing will review it.');
      onClose();
    } catch (error) {
      const env = envelopeOf(error);
      const tone = env.status === 409 || env.status === 429 ? 'warning' : 'danger';
      setProblem({ explanation: explainReportError(error), tone });
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="report-package-title">
      <DialogHeader titleId="report-package-title">Report {item.display_name}</DialogHeader>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-xs leading-5 text-text-secondary">
          Reports go to the moderators of this listing (platform admins for public and personal packages, organization owners and admins for org packages). Your identity is visible to them, not to the publisher.
        </p>
        <Select
          title="Reason"
          value={reason}
          onValueChange={(value) => setReason(value as ReportReason)}
          placeholder="Choose a reason"
          options={REPORT_REASONS.map((value) => ({ value, label: REPORT_REASON_LABELS[value] }))}
        />
        <div>
          <Textarea
            title="Details (optional)"
            value={details}
            onChange={(event) => setDetails(event.target.value.slice(0, MAX_REPORT_DETAILS))}
            maxLength={MAX_REPORT_DETAILS}
            placeholder="What did you see? Point to files or behavior. Do not paste live secrets."
          />
          <p className="mt-1 text-right text-2xs text-text-tertiary" aria-live="polite">
            {details.length} / {MAX_REPORT_DETAILS}
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-text-secondary">
          <input type="checkbox" checked={thisVersion} onChange={(event) => setThisVersion(event.target.checked)} />
          Only version {item.version || item.version_id} (uncheck to report the whole package)
        </label>
        {problem && <ExplanationAlert explanation={problem.explanation} tone={problem.tone} />}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="danger" disabled={!reason || report.isPending} loading={report.isPending}>Submit report</Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
