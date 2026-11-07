import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './client';
import { createWorkflowApi } from './workflows';
import { plan, started, template } from '../projects/workflows/fixtures';

afterEach(() => vi.unstubAllGlobals());

function stub(body: unknown, status = 200) {
  const f = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  vi.stubGlobal('fetch', f);
  return f;
}

describe('workflow api', () => {
  const api = createWorkflowApi('https://x.test', 'k');

  it('lists and writes project templates', async () => {
    const f = stub([template()]);
    await api.listTemplates({ kind: 'project', id: 'proj 1' });
    expect(f.mock.calls[0][0]).toBe('https://x.test/api/v1/projects/proj%201/workflow-templates');
    await api.createTemplate({ kind: 'project', id: 'p' }, { name: 'A', definition: { steps: [] } });
    expect(f.mock.calls[1][1].method).toBe('POST');
    expect(JSON.parse(f.mock.calls[1][1].body)).toEqual({ name: 'A', definition: { steps: [] } });
    await api.updateTemplate({ kind: 'project', id: 'p' }, 'wft_1', { name: 'A', definition: {} });
    expect(f.mock.calls[2][0]).toBe('https://x.test/api/v1/projects/p/workflow-templates/wft_1');
    expect(f.mock.calls[2][1].method).toBe('PUT');
    expect(f.mock.calls[0][1].headers.Authorization).toBe('Bearer k');
  });

  it('uses the org routes and treats a 204 disable as success', async () => {
    const f = stub(undefined, 204);
    await expect(api.disableTemplate({ kind: 'org', id: 'org_1' }, 'wft_9')).resolves.toBeUndefined();
    expect(f.mock.calls[0][0]).toBe('https://x.test/api/v1/orgs/org_1/workflow-templates/wft_9');
    expect(f.mock.calls[0][1].method).toBe('DELETE');
  });

  it('starts a workflow and reads the plan', async () => {
    const f = stub(started());
    const res = await api.startWorkflow('p', { template_id: 'wft_1', title: 'T', token_budget: 5 });
    expect(f.mock.calls[0][0]).toBe('https://x.test/api/v1/projects/p/workflows');
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ template_id: 'wft_1', title: 'T', token_budget: 5 });
    expect(res.loop_id).toBe('tk_wfloop');
    stub(plan());
    const got = await api.getPlan('p', 'tk_wfloop');
    expect(got.steps[1].ready_children[0].brief_markdown).toContain('Known traps');
  });

  it('turns the server validation error into an ApiError with the message', async () => {
    stub({ detail: { error: 'invalid_workflow_definition', message: 'steps: List should have at least 1 item' } }, 422);
    const err = await api.createTemplate({ kind: 'project', id: 'p' }, { name: 'A', definition: {} }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.message).toContain('at least 1 item');
  });
});
