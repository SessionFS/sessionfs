import { useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import {
  createWorkflowApi,
  type StartWorkflowBody,
  type TemplateTarget,
  type TemplateWrite,
} from '../api/workflows';

function useApi() {
  const { auth } = useAuth();
  const api = useMemo(() => (auth ? createWorkflowApi(auth.baseUrl, auth.apiKey) : null), [auth]);
  return { api, enabled: !!api };
}

const targetKey = (t: TemplateTarget) => ['workflowTemplates', t.kind, t.id] as const;

export function useWorkflowTemplates(target: TemplateTarget | null, enabled = true) {
  const { api, enabled: ready } = useApi();
  return useQuery({
    queryKey: target ? targetKey(target) : ['workflowTemplates', 'none'],
    queryFn: () => api!.listTemplates(target!),
    enabled: ready && enabled && !!target,
    staleTime: 30_000,
  });
}

export function useSaveWorkflowTemplate(target: TemplateTarget) {
  const { api } = useApi();
  const qc = useQueryClient();
  return useMutation({
    /** `templateId` set = a new version of that template; unset = a new template. */
    mutationFn: (v: { templateId?: string; body: TemplateWrite }) =>
      v.templateId ? api!.updateTemplate(target, v.templateId, v.body) : api!.createTemplate(target, v.body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['workflowTemplates'] }),
  });
}

export function useDisableWorkflowTemplate(target: TemplateTarget) {
  const { api } = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (templateId: string) => api!.disableTemplate(target, templateId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['workflowTemplates'] }),
  });
}

export function useStartWorkflow(projectId: string) {
  const { api } = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: StartWorkflowBody) => api!.startWorkflow(projectId, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['loops', projectId] });
      void qc.invalidateQueries({ queryKey: ['workflowPlan', projectId] });
    },
  });
}

/** The plan is computed from stored rows on every call; poll lightly like the board. */
export function useWorkflowPlan(projectId: string | undefined, loopId: string | undefined) {
  const { api, enabled } = useApi();
  return useQuery({
    queryKey: ['workflowPlan', projectId, loopId],
    queryFn: () => api!.getPlan(projectId!, loopId!),
    enabled: enabled && !!projectId && !!loopId,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}
