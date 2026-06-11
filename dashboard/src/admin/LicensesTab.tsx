import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { HelmLicense } from '../api/client';
import { Select, Table } from '../components/ui';

const STATUS_STYLES: Record<string, string> = {
  active: 'bg-green-500/10 text-green-500 border-green-500/30',
  expired: 'bg-red-500/10 text-red-500 border-red-500/30',
  revoked: 'bg-neutral-500/10 text-neutral-400 border-neutral-500/30',
  trial: 'bg-yellow-500/10 text-yellow-500 border-yellow-500/30',
};

export default function LicensesTab() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState('all');

  const { data, isLoading, error } = useQuery({
    queryKey: ['admin-licenses', statusFilter],
    queryFn: () => auth!.client.adminListLicenses(statusFilter),
    enabled: !!auth,
  });

  const revokeMutation = useMutation({
    mutationFn: ({ key, reason }: { key: string; reason: string }) =>
      auth!.client.adminRevokeLicense(key, reason),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-licenses'] }),
  });

  const licenses = data?.licenses ?? [];

  return (
    <div>
      {/* Filter */}
      <div className="mb-4">
        <Select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          options={[
            { value: 'all', label: 'All statuses' },
            { value: 'active', label: 'Active' },
            { value: 'expired', label: 'Expired' },
            { value: 'revoked', label: 'Revoked' },
          ]}
        />
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
          Failed to load licenses: {String(error)}
        </div>
      )}

      {isLoading && (
        <div className="text-[var(--text-tertiary)] text-sm py-4">Loading licenses...</div>
      )}

      {!isLoading && licenses.length === 0 && (
        <div className="text-center py-12 text-[var(--text-tertiary)] text-sm">No licenses found</div>
      )}

      {licenses.length > 0 && (
        <Table
          columns={[
            {
              key: 'org',
              header: 'Organization',
              render: (lic) => (
                <span className="text-[var(--text-primary)]">{lic.org_name}</span>
              ),
            },
            {
              key: 'contact',
              header: 'Contact',
              render: (lic) => (
                <span className="text-[var(--text-secondary)]">{lic.contact_email}</span>
              ),
            },
            {
              key: 'type',
              header: 'Type',
              width: 'w-20',
              render: (lic) => (
                <span className="text-xs text-[var(--text-tertiary)]">{lic.license_type}</span>
              ),
            },
            {
              key: 'tier',
              header: 'Tier',
              width: 'w-20',
              render: (lic) => (
                <span className="text-xs text-[var(--text-secondary)] capitalize">{lic.tier}</span>
              ),
            },
            {
              key: 'seats',
              header: 'Seats',
              width: 'w-16',
              render: (lic) => (
                <span className="tabular-nums">{lic.seats_limit}</span>
              ),
            },
            {
              key: 'status',
              header: 'Status',
              width: 'w-24',
              render: (lic) => {
                const statusStyle = STATUS_STYLES[lic.effective_status] || STATUS_STYLES['active'];
                return (
                  <span className={`px-2 py-0.5 text-xs font-medium border rounded-full ${statusStyle}`}>
                    {lic.effective_status}
                  </span>
                );
              },
            },
            {
              key: 'expires',
              header: 'Expires',
              width: 'w-28',
              render: (lic) => (
                <span className="text-xs text-[var(--text-tertiary)]">
                  {lic.expires_at ? lic.expires_at.slice(0, 10) : 'Never'}
                </span>
              ),
            },
            {
              key: 'actions',
              header: 'Actions',
              width: 'w-20',
              render: (lic) =>
                lic.effective_status === 'active' ? (
                  <button
                    onClick={() => {
                      const reason = prompt('Reason for revocation:');
                      if (reason) revokeMutation.mutate({ key: lic.id, reason });
                    }}
                    disabled={revokeMutation.isPending}
                    className="text-xs text-red-500 hover:underline disabled:opacity-50"
                  >
                    Revoke
                  </button>
                ) : (
                  <span className="text-xs text-[var(--text-tertiary)]">—</span>
                ),
            },
          ]}
          data={licenses}
          rowKey={(lic) => lic.id}
        />
      )}
    </div>
  );
}
