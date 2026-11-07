import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { plan, started, templates } from '../projects/workflows/fixtures';
import { useSaveWorkflowTemplate, useStartWorkflow, useWorkflowPlan, useWorkflowTemplates } from './useWorkflows';

vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ auth: { apiKey: 'sk', baseUrl: 'http://test' } }) }));

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const spy = vi.spyOn(qc, 'invalidateQueries');
  const Wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  return { Wrapper, spy };
}

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn(async (url: string) => {
    const body = url.endsWith('/plan') ? plan() : url.endsWith('/workflows') ? started() : templates();
    return { ok: true, status: 200, json: async () => body, text: async () => '' };
  });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe('workflow hooks', () => {
  it('loads templates only when enabled', async () => {
    const { Wrapper } = wrap();
    const off = renderHook(() => useWorkflowTemplates({ kind: 'project', id: 'p' }, false), { wrapper: Wrapper });
    expect(off.result.current.fetchStatus).toBe('idle');
    const on = renderHook(() => useWorkflowTemplates({ kind: 'project', id: 'p' }), { wrapper: Wrapper });
    await waitFor(() => expect(on.result.current.data).toHaveLength(2));
    expect(fetchMock.mock.calls[0][0]).toBe('http://test/api/v1/projects/p/workflow-templates');
  });

  it('reads the plan', async () => {
    const { Wrapper } = wrap();
    const { result } = renderHook(() => useWorkflowPlan('p', 'tk_wfloop'), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.data?.ready_steps).toEqual(['implement']));
  });

  it('refreshes the loops and plans after a start, and templates after a save', async () => {
    const { Wrapper, spy } = wrap();
    const start = renderHook(() => useStartWorkflow('p'), { wrapper: Wrapper });
    const res = await start.result.current.mutateAsync({ template_id: 'wft_1', title: 'T' });
    expect(res.loop_id).toBe('tk_wfloop');
    expect(spy).toHaveBeenCalledWith({ queryKey: ['loops', 'p'] });
    expect(spy).toHaveBeenCalledWith({ queryKey: ['workflowPlan', 'p'] });
    const save = renderHook(() => useSaveWorkflowTemplate({ kind: 'project', id: 'p' }), { wrapper: Wrapper });
    await save.result.current.mutateAsync({ templateId: 'wft_1', body: { name: 'A', definition: {} } });
    expect(fetchMock.mock.calls.at(-1)?.[1].method).toBe('PUT');
    expect(spy).toHaveBeenCalledWith({ queryKey: ['workflowTemplates'] });
  });
});
