import AutoAuditCard from './AutoAuditCard';
import AuditJudgeSettings from './AuditJudgeSettings';
import DlpPolicyCard from './DlpPolicyCard';

/**
 * Data protection: when sessions are audited, which model reviews them
 * (the "Audit Judge"), and the org-level DLP policy for secrets and PHI.
 */
export default function DataProtectionSection() {
  return (
    <div className="space-y-5">
      <AutoAuditCard />
      <AuditJudgeSettings />
      <DlpPolicyCard />
    </div>
  );
}
