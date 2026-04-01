import { useState, useCallback, useEffect, useRef } from 'react';
import {
  useAdminLicenses,
  useCreateLicense,
  useExtendLicense,
  useRevokeLicense,
  useLicenseHistory,
} from '../hooks/useAdminLicenses';
import type { HelmLicense, CreateLicenseRequest } from '../api/client';
import RelativeDate from '../components/RelativeDate';

const STATUS_COLORS: Record<string, string> = {
  active: 'bg-green-500/20 text-green-400',
  trial: 'bg-blue-500/20 text-blue-400',
  expiring: 'bg-yellow-500/20 text-yellow-400',
  expired: 'bg-red-500/20 text-red-400',
  revoked: 'bg-gray-500/20 text-gray-400',
};

const TYPE_COLORS: Record<string, string> = {
  trial: 'border-blue-500/50 text-blue-400',
  paid: 'border-green-500/50 text-green-400',
  internal: 'border-gray-500/50 text-gray-400',
};

function truncateKey(key: string): string {
  if (key.length <= 12) return key;
  return `${key.slice(0, 8)}...${key.slice(-4)}`;
}

function copyToClipboard(text: string) {
  void navigator.clipboard.writeText(text);
}

// ---------------------------------------------------------------------------
// Modal shell
// ---------------------------------------------------------------------------

function Modal({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) cancelRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative bg-bg-secondary border border-border rounded-lg shadow-xl max-w-lg w-full mx-4 p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-text-primary">{title}</h3>
          <button
            ref={cancelRef}
            onClick={onClose}
            className="text-text-muted hover:text-text-secondary transition-colors"
          >
            &#10005;
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create license modal
// ---------------------------------------------------------------------------

function CreateLicenseModal({
  open,
  mode,
  onClose,
}: {
  open: boolean;
  mode: 'trial' | 'paid';
  onClose: () => void;
}) {
  const createLicense = useCreateLicense();
  const [orgName, setOrgName] = useState('');
  const [email, setEmail] = useState('');
  const [tier, setTier] = useState('team');
  const [seats, setSeats] = useState(mode === 'trial' ? 25 : 50);
  const [days, setDays] = useState(14);
  const [notes, setNotes] = useState('');

  const handleSubmit = useCallback(() => {
    const data: CreateLicenseRequest = {
      org_name: orgName,
      contact_email: email,
      license_type: mode,
      tier,
      seats_limit: seats,
      ...(mode === 'trial' ? { days } : {}),
      ...(notes ? { notes } : {}),
    };
    createLicense.mutate(data, {
      onSuccess: () => {
        setOrgName('');
        setEmail('');
        setTier('team');
        setSeats(mode === 'trial' ? 25 : 50);
        setDays(14);
        setNotes('');
        onClose();
      },
    });
  }, [orgName, email, mode, tier, seats, days, notes, createLicense, onClose]);

  const canSubmit = orgName.trim() !== '' && email.trim() !== '';

  return (
    <Modal open={open} title={mode === 'trial' ? 'Create Trial License' : 'Create License'} onClose={onClose}>
      <div className="space-y-3">
        <div>
          <label className="block text-sm text-text-secondary mb-1">Organization Name</label>
          <input
            type="text"
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
            className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
            placeholder="Acme Corp"
          />
        </div>
        <div>
          <label className="block text-sm text-text-secondary mb-1">Contact Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
            placeholder="admin@acme.com"
          />
        </div>
        {mode === 'paid' && (
          <div>
            <label className="block text-sm text-text-secondary mb-1">Tier</label>
            <select
              value={tier}
              onChange={(e) => setTier(e.target.value)}
              className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent"
            >
              <option value="team">team</option>
              <option value="enterprise">enterprise</option>
            </select>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          {mode === 'trial' && (
            <div>
              <label className="block text-sm text-text-secondary mb-1">Days</label>
              <input
                type="number"
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                min={1}
                className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
              />
            </div>
          )}
          <div>
            <label className="block text-sm text-text-secondary mb-1">Seats</label>
            <input
              type="number"
              value={seats}
              onChange={(e) => setSeats(Number(e.target.value))}
              min={1}
              className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
            />
          </div>
        </div>
        <div>
          <label className="block text-sm text-text-secondary mb-1">Notes (optional)</label>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent resize-none"
          />
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm border border-border rounded hover:bg-bg-tertiary transition-colors text-text-secondary"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit || createLicense.isPending}
            className="px-4 py-2 text-sm rounded font-medium bg-accent hover:bg-accent/90 text-white transition-colors disabled:opacity-50"
          >
            {createLicense.isPending ? 'Creating...' : 'Create'}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Extend license modal
// ---------------------------------------------------------------------------

function ExtendModal({
  open,
  licenseKey,
  onClose,
}: {
  open: boolean;
  licenseKey: string;
  onClose: () => void;
}) {
  const extendLicense = useExtendLicense();
  const [days, setDays] = useState(30);

  const handleSubmit = useCallback(() => {
    extendLicense.mutate(
      { key: licenseKey, days },
      { onSuccess: () => { setDays(30); onClose(); } },
    );
  }, [licenseKey, days, extendLicense, onClose]);

  return (
    <Modal open={open} title="Extend License" onClose={onClose}>
      <div className="space-y-3">
        <div>
          <label className="block text-sm text-text-secondary mb-1">Days to Add</label>
          <input
            type="number"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            min={1}
            className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
          />
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm border border-border rounded hover:bg-bg-tertiary transition-colors text-text-secondary"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={extendLicense.isPending}
            className="px-4 py-2 text-sm rounded font-medium bg-accent hover:bg-accent/90 text-white transition-colors disabled:opacity-50"
          >
            {extendLicense.isPending ? 'Extending...' : 'Extend'}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Revoke license modal
// ---------------------------------------------------------------------------

function RevokeModal({
  open,
  licenseKey,
  onClose,
}: {
  open: boolean;
  licenseKey: string;
  onClose: () => void;
}) {
  const revokeLicense = useRevokeLicense();
  const [reason, setReason] = useState('');

  const handleSubmit = useCallback(() => {
    revokeLicense.mutate(
      { key: licenseKey, reason },
      { onSuccess: () => { setReason(''); onClose(); } },
    );
  }, [licenseKey, reason, revokeLicense, onClose]);

  return (
    <Modal open={open} title="Revoke License" onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm text-text-secondary">
          This will permanently revoke the license. The license key will no longer validate.
        </p>
        <div>
          <label className="block text-sm text-text-secondary mb-1">Reason</label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            className="w-full px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent resize-none"
            placeholder="Reason for revocation..."
          />
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm border border-border rounded hover:bg-bg-tertiary transition-colors text-text-secondary"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!reason.trim() || revokeLicense.isPending}
            className="px-4 py-2 text-sm rounded font-medium bg-red-600 hover:bg-red-700 text-white transition-colors disabled:opacity-50"
          >
            {revokeLicense.isPending ? 'Revoking...' : 'Revoke'}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Validation history panel
// ---------------------------------------------------------------------------

function ValidationHistory({ licenseKey }: { licenseKey: string }) {
  const { data, isLoading } = useLicenseHistory(licenseKey);

  if (isLoading) return <div className="text-text-muted text-sm py-2">Loading history...</div>;
  if (!data || data.validations.length === 0)
    return <div className="text-text-muted text-sm py-2">No validation history</div>;

  return (
    <div className="border border-border rounded overflow-hidden mt-3">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-bg-secondary text-text-secondary text-sm uppercase tracking-wider">
            <th className="px-3 py-1.5 text-left">Timestamp</th>
            <th className="px-3 py-1.5 text-left">Cluster ID</th>
            <th className="px-3 py-1.5 text-left">IP</th>
            <th className="px-3 py-1.5 text-left">Result</th>
            <th className="px-3 py-1.5 text-left">Version</th>
          </tr>
        </thead>
        <tbody>
          {data.validations.slice(0, 20).map((v) => (
            <tr key={v.id} className="border-t border-border">
              <td className="px-3 py-1.5 text-text-muted">
                <RelativeDate iso={v.validated_at} />
              </td>
              <td className="px-3 py-1.5 text-text-secondary font-mono text-sm">
                {v.cluster_id || '-'}
              </td>
              <td className="px-3 py-1.5 text-text-secondary font-mono text-sm">
                {v.ip_address || '-'}
              </td>
              <td className="px-3 py-1.5">
                <span
                  className={`px-2 py-0.5 rounded text-sm font-medium ${
                    v.result === 'valid'
                      ? 'bg-green-500/20 text-green-400'
                      : 'bg-red-500/20 text-red-400'
                  }`}
                >
                  {v.result}
                </span>
              </td>
              <td className="px-3 py-1.5 text-text-muted">{v.version || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// License row
// ---------------------------------------------------------------------------

function LicenseRow({
  license,
  expanded,
  onToggle,
  onExtend,
  onRevoke,
}: {
  license: HelmLicense;
  expanded: boolean;
  onToggle: () => void;
  onExtend: () => void;
  onRevoke: () => void;
}) {
  const statusColor = STATUS_COLORS[license.effective_status] || 'bg-gray-500/20 text-gray-400';
  const typeColor = TYPE_COLORS[license.license_type] || 'border-gray-500/50 text-gray-400';
  const canExtend =
    license.effective_status === 'trial' ||
    license.effective_status === 'expiring' ||
    license.effective_status === 'active';
  const canRevoke = license.effective_status !== 'revoked';

  return (
    <>
      <tr
        onClick={onToggle}
        className="border-t border-border hover:bg-bg-tertiary cursor-pointer transition-colors"
      >
        <td className="px-3 py-2 font-mono text-sm text-text-secondary" title={license.id}>
          {truncateKey(license.id)}
        </td>
        <td className="px-3 py-2 text-text-primary">{license.org_name}</td>
        <td className="px-3 py-2">
          <span className={`px-2 py-0.5 rounded border text-sm font-medium ${typeColor}`}>
            {license.license_type}
          </span>
        </td>
        <td className="px-3 py-2">
          <span className="px-2 py-0.5 rounded bg-purple-500/20 text-purple-400 text-sm font-medium">
            {license.tier}
          </span>
        </td>
        <td className="px-3 py-2 text-text-secondary tabular-nums text-right">{license.seats_limit}</td>
        <td className="px-3 py-2">
          <span className={`px-2 py-0.5 rounded text-sm font-medium ${statusColor}`}>
            {license.effective_status}
          </span>
        </td>
        <td className="px-3 py-2 text-text-muted text-sm">
          {license.expires_at ? <RelativeDate iso={license.expires_at} /> : <span>-</span>}
        </td>
        <td className="px-3 py-2 text-text-muted text-sm">
          <RelativeDate iso={license.last_validated_at} />
        </td>
        <td className="px-3 py-2">
          <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
            {canExtend && (
              <button
                onClick={onExtend}
                className="px-2 py-0.5 text-sm border border-border rounded hover:bg-bg-tertiary transition-colors text-text-secondary"
              >
                Extend
              </button>
            )}
            {canRevoke && (
              <button
                onClick={onRevoke}
                className="px-2 py-0.5 text-sm border border-red-500/30 rounded hover:bg-red-500/10 transition-colors text-red-400"
              >
                Revoke
              </button>
            )}
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="border-t border-border bg-bg-tertiary">
          <td colSpan={9} className="px-4 py-3">
            <div className="space-y-3">
              {/* Full key */}
              <div className="flex items-center gap-2">
                <span className="text-sm text-text-muted">License Key:</span>
                <code className="text-sm text-text-secondary font-mono bg-bg-secondary px-2 py-0.5 rounded">
                  {license.id}
                </code>
                <button
                  onClick={() => copyToClipboard(license.id)}
                  className="px-2 py-0.5 text-sm border border-border rounded hover:bg-bg-secondary transition-colors text-text-muted"
                >
                  Copy
                </button>
              </div>

              {/* Detail grid */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                <div>
                  <span className="text-text-muted block">Email</span>
                  <span className="text-text-secondary">{license.contact_email}</span>
                </div>
                <div>
                  <span className="text-text-muted block">Status</span>
                  <span className="text-text-secondary">{license.status}</span>
                </div>
                <div>
                  <span className="text-text-muted block">Validations</span>
                  <span className="text-text-secondary tabular-nums">{license.validation_count}</span>
                </div>
                <div>
                  <span className="text-text-muted block">Created</span>
                  <span className="text-text-secondary">
                    <RelativeDate iso={license.created_at} />
                  </span>
                </div>
              </div>

              {license.notes && (
                <div className="text-sm">
                  <span className="text-text-muted">Notes:</span>{' '}
                  <span className="text-text-secondary">{license.notes}</span>
                </div>
              )}

              {/* Validation history */}
              <div>
                <h4 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-1">
                  Validation History
                </h4>
                <ValidationHistory licenseKey={license.id} />
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main LicensesTab
// ---------------------------------------------------------------------------

export default function LicensesTab() {
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [createMode, setCreateMode] = useState<'trial' | 'paid' | null>(null);
  const [extendKey, setExtendKey] = useState<string | null>(null);
  const [revokeKey, setRevokeKey] = useState<string | null>(null);

  const { data, isLoading, error } = useAdminLicenses(statusFilter);

  return (
    <div>
      {/* Header with actions */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent"
          >
            <option value="all">All Statuses</option>
            <option value="active">Active</option>
            <option value="trial">Trial</option>
            <option value="expiring">Expiring</option>
            <option value="expired">Expired</option>
            <option value="revoked">Revoked</option>
          </select>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setCreateMode('trial')}
            className="px-3 py-1.5 text-sm border border-blue-500/50 text-blue-400 rounded hover:bg-blue-500/10 transition-colors"
          >
            Create Trial
          </button>
          <button
            onClick={() => setCreateMode('paid')}
            className="px-3 py-1.5 text-sm bg-accent hover:bg-accent/90 text-white rounded transition-colors"
          >
            Create License
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
          Failed to load licenses: {String(error)}
        </div>
      )}

      {isLoading && <div className="text-text-muted text-sm">Loading licenses...</div>}

      {data && data.licenses.length > 0 && (
        <div className="border border-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-bg-secondary text-text-secondary text-sm uppercase tracking-wider">
                <th className="px-3 py-2 text-left">Key</th>
                <th className="px-3 py-2 text-left">Org</th>
                <th className="px-3 py-2 text-left">Type</th>
                <th className="px-3 py-2 text-left">Tier</th>
                <th className="px-3 py-2 text-right">Seats</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-left">Expiry</th>
                <th className="px-3 py-2 text-left">Last Seen</th>
                <th className="px-3 py-2 text-left">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.licenses.map((lic) => (
                <LicenseRow
                  key={lic.id}
                  license={lic}
                  expanded={expandedKey === lic.id}
                  onToggle={() => setExpandedKey((prev) => (prev === lic.id ? null : lic.id))}
                  onExtend={() => setExtendKey(lic.id)}
                  onRevoke={() => setRevokeKey(lic.id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && data.licenses.length === 0 && !isLoading && (
        <div className="text-center py-8 text-text-muted text-sm">No licenses found</div>
      )}

      {data && (
        <div className="mt-2 text-sm text-text-muted">
          {data.licenses.length} license{data.licenses.length !== 1 ? 's' : ''}
        </div>
      )}

      {/* Modals */}
      <CreateLicenseModal
        open={createMode !== null}
        mode={createMode || 'trial'}
        onClose={() => setCreateMode(null)}
      />
      {extendKey && (
        <ExtendModal
          open={!!extendKey}
          licenseKey={extendKey}
          onClose={() => setExtendKey(null)}
        />
      )}
      {revokeKey && (
        <RevokeModal
          open={!!revokeKey}
          licenseKey={revokeKey}
          onClose={() => setRevokeKey(null)}
        />
      )}
    </div>
  );
}
