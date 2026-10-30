import { useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { createReviewLoopApi, type LoopBudgetPatch, type Range } from '../api/reviewLoop';

/**
 * Review Loop reads. Light polling only (no websockets): the board refreshes
 * every 30 s while open, the slower rollups every 2 min.
 */
function useApi() {
  const { auth } = useAuth();
  const api = useMemo(
    () => (auth ? createReviewLoopApi(auth.baseUrl, auth.apiKey) : null),
    [auth],
  );
  return { api, enabled: !!api };
}

export function useLoops(projectId: string | undefined) {
  const { api, enabled } = useApi();
  return useQuery({
    queryKey: ['loops', projectId],
    queryFn: () => api!.listLoops(projectId!),
    enabled: enabled && !!projectId,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function useLoopInsights(projectId: string | undefined, range: Range = {}) {
  const { api, enabled } = useApi();
  return useQuery({
    queryKey: ['loopInsights', projectId, range.from ?? null, range.to ?? null],
    queryFn: () => api!.getInsights(projectId!, range),
    enabled: enabled && !!projectId,
    staleTime: 60_000,
    refetchInterval: 120_000,
  });
}

export function useStory(projectId: string | undefined, ticketId: string | null) {
  const { api, enabled } = useApi();
  return useQuery({
    queryKey: ['loopStory', projectId, ticketId],
    queryFn: () => api!.getStory(projectId!, ticketId!),
    enabled: enabled && !!projectId && !!ticketId,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function useTicketSpend(projectId: string | undefined, ticketId: string | null) {
  const { api, enabled } = useApi();
  return useQuery({
    queryKey: ['loopTicketSpend', projectId, ticketId],
    queryFn: () => api!.getTicketSpend(projectId!, ticketId!),
    enabled: enabled && !!projectId && !!ticketId,
    staleTime: 60_000,
  });
}

export function useChecklist(projectId: string | undefined) {
  const { api, enabled } = useApi();
  return useQuery({
    queryKey: ['loopChecklist', projectId],
    queryFn: () => api!.listChecklist(projectId!),
    enabled: enabled && !!projectId,
    staleTime: 30_000,
  });
}

/** Loop writes refresh the board; checklist writes refresh the list and the rollups. */
export function useUpdateLoop(projectId: string) {
  const { api } = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { loopId: string; body: LoopBudgetPatch }) => api!.updateLoop(projectId, v.loopId, v.body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['loops', projectId] }),
  });
}

export function useCloseLoopItem(projectId: string) {
  const { api } = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { loopId: string; ticketId: string }) => api!.closeLoopItem(projectId, v.loopId, v.ticketId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['loops', projectId] }),
  });
}

function useChecklistInvalidate(projectId: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ['loopChecklist', projectId] });
    void qc.invalidateQueries({ queryKey: ['loopInsights', projectId] });
  };
}

export function useSeedChecklist(projectId: string) {
  const { api } = useApi();
  const done = useChecklistInvalidate(projectId);
  return useMutation({ mutationFn: () => api!.seedChecklist(projectId), onSuccess: done });
}

export function usePatchChecklistItem(projectId: string) {
  const { api } = useApi();
  const done = useChecklistInvalidate(projectId);
  return useMutation({
    mutationFn: (v: { itemId: string; status: 'active' | 'retired' }) =>
      api!.patchChecklistItem(projectId, v.itemId, v.status),
    onSuccess: done,
  });
}
