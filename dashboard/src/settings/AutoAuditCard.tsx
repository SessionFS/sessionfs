import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { Card } from '../components/ui';

export default function AutoAuditCard() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ['auditTrigger'],
    queryFn: () => auth!.client.getAuditTrigger(),
    enabled: !!auth,
    staleTime: 30_000,
  });

  const update = useMutation({
    mutationFn: (trigger: string) => auth!.client.updateAuditTrigger(trigger),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['auditTrigger'] }),
  });

  const current = data?.trigger || 'manual';

  return (
    <Card level="elevated" className="p-5">
      <h2 className="text-lg font-semibold text-text-primary mb-3">Auto-audit</h2>
      <div className="space-y-2">
        {[
          { value: 'manual', label: 'Manual only -- run audits when you choose' },
          { value: 'on_sync', label: 'On sync -- automatically audit after each push' },
          { value: 'on_pr', label: 'On PR/MR -- audit when a pull/merge request is opened' },
        ].map(({ value, label }) => (
          <label key={value} className="flex items-center gap-2.5 text-sm text-text-secondary cursor-pointer">
            <input type="radio" name="audit-trigger" checked={current === value}
              onChange={() => update.mutate(value)} disabled={update.isPending}
              className="text-brand focus-visible:ring-[var(--brand)]" />
            {label}
          </label>
        ))}
      </div>
    </Card>
  );
}
