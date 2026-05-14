import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { AgentRun } from '../api/client';

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
