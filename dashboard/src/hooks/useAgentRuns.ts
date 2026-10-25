import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type {
  AgentRun,
  AgentRunCompareResult,
  AgentRunReplayInput,
} from '../api/client';

/**
 * React-Query hooks for v0.10.2 AgentRuns (Team+).
 *
 * Read-only on the dashboard. AgentRuns are CI-driven — they're
 * created and completed by `sfs agent run` / `sfs agent complete`
 * inside a CI runner. The dashboard surfaces them as an audit trail.
 */

export interface AgentRunFilters {
  persona_name?: string;
  status?: string;
  trigger_source?: string;
  ticket_id?: string;
  limit?: number;
}

export function useAgentRuns(
  projectId: string | undefined,
  filters: AgentRunFilters = {},
) {
  const { auth } = useAuth();
  return useQuery<AgentRun[]>({
    queryKey: ['agentRuns', projectId, filters],
    queryFn: () => auth!.client.listAgentRuns(projectId!, filters),
    enabled: !!auth && !!projectId,
    // Runs reach terminal state quickly; refetch every 15s while the
    // page is open so a CI-completed run shows up without a manual
    // reload.
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function useAgentRun(
  projectId: string | undefined,
  runId: string | undefined,
) {
  const { auth } = useAuth();
  return useQuery<AgentRun>({
    queryKey: ['agentRun', projectId, runId],
    queryFn: () => auth!.client.getAgentRun(projectId!, runId!),
    enabled: !!auth && !!projectId && !!runId,
    staleTime: 15_000,
  });
}

/**
 * GitHub #95 — replay a run with edited prompt/ticket/context. Reuses the
 * AgentRuns query cache so the new queued run appears without a reload.
 */
export function useReplayAgentRun(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, body }: { runId: string; body: AgentRunReplayInput }) =>
      auth!.client.replayAgentRun(projectId!, runId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['agentRuns', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['agentRun', projectId] });
    },
  });
}

/** GitHub #95 — findings diff between two runs in the same project. */
export function useCompareAgentRuns(
  projectId: string | undefined,
  aId: string | undefined,
  bId: string | undefined,
) {
  const { auth } = useAuth();
  return useQuery<AgentRunCompareResult>({
    queryKey: ['agentRunCompare', projectId, aId, bId],
    queryFn: () => auth!.client.compareAgentRuns(projectId!, aId!, bId!),
    enabled: !!auth && !!projectId && !!aId && !!bId,
    staleTime: 15_000,
  });
}
