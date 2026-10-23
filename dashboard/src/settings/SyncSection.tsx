import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { Card } from '../components/ui';

export default function SyncSection() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  const { data: syncSettings, isLoading } = useQuery({
    queryKey: ['syncSettings'],
    queryFn: () => auth!.client.getSyncSettings(),
    enabled: !!auth,
    staleTime: 30_000,
  });

  const updateMode = useMutation({
    mutationFn: (mode: string) => auth!.client.updateSyncSettings(mode),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['syncSettings'] }),
  });

  const currentMode = syncSettings?.mode || 'off';

  return (
    <Card level="elevated" className="p-5">
      <h2 className="text-lg font-semibold text-text-primary mb-2">Autosync</h2>
      <p className="text-sm text-text-tertiary mb-3">
        Automatically sync sessions to the cloud. The daemon pushes changes after a short debounce period.
      </p>

      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : (
        <div className="space-y-2">
          {(['off', 'all', 'selective'] as const).map((mode) => (
            <label key={mode} className="flex items-center gap-2.5 text-sm text-text-secondary cursor-pointer">
              <input
                type="radio"
                name="sync-mode"
                checked={currentMode === mode}
                onChange={() => updateMode.mutate(mode)}
                disabled={updateMode.isPending}
                className="text-brand focus-visible:ring-[var(--brand)]"
              />
              {mode === 'off' && 'Off -- Manual sync only (sfs push)'}
              {mode === 'all' && 'All sessions -- Sync everything automatically'}
              {mode === 'selective' && 'Selective -- Choose which sessions to sync'}
            </label>
          ))}

          {updateMode.isError && (
            <div className="text-sm text-danger mt-1">Failed to update sync mode.</div>
          )}
        </div>
      )}
    </Card>
  );
}
