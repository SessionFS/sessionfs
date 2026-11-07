import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/client';
import { axe } from '../../test/axe';
import { WorkflowGraph } from './WorkflowGraph';
import { layoutSteps } from './graph';
import { TemplatesPanel } from './TemplatesPanel';
import { TemplateEditorDialog } from './TemplateEditorDialog';
import { StartWorkflowDialog } from './StartWorkflowDialog';
import { WorkflowLanes } from './WorkflowLanes';
import { LoopBoard } from '../loop/LoopBoard';
import { loops } from '../loop/fixtures';
import { orgTemplate, plan, started, template, templates, workflowLoop } from './fixtures';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useWorkflowTemplates: vi.fn(),
    useSaveWorkflowTemplate: vi.fn(),
    useDisableWorkflowTemplate: vi.fn(),
    useStartWorkflow: vi.fn(),
    useWorkflowPlan: vi.fn(),
  },
}));
vi.mock('../../hooks/useWorkflows', () => hooks);

const ok = (data: unknown) => ({ data, isLoading: false, error: null });
const mut = (over: Record<string, unknown> = {}) => ({ mutate: vi.fn(), mutateAsync: vi.fn().mockResolvedValue({}), isPending: false, error: null, ...over });

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useWorkflowTemplates.mockReturnValue(ok(templates()));
  hooks.useSaveWorkflowTemplate.mockReturnValue(mut());
  hooks.useDisableWorkflowTemplate.mockReturnValue(mut());
  hooks.useStartWorkflow.mockReturnValue(mut({ mutateAsync: vi.fn().mockResolvedValue(started()) }));
  hooks.useWorkflowPlan.mockReturnValue(ok(plan()));
});

describe('graph layout', () => {
  it('places steps left to right by dependency', () => {
    const steps = template().definition!.steps;
    const l = layoutSteps(steps);
    expect(l.nodes.map((n) => n.column)).toEqual([0, 1, 2, 3, 4]);
    expect(l.edges).toHaveLength(4);
  });
  it('stacks siblings in one column and survives a cycle', () => {
    const base = template().definition!.steps[0];
    const l = layoutSteps([
      { ...base, id: 'a', depends_on: [] },
      { ...base, id: 'b', depends_on: ['a'] },
      { ...base, id: 'c', depends_on: ['a'] },
    ]);
    expect(l.nodes.filter((n) => n.column === 1).map((n) => n.row)).toEqual([0, 1]);
    expect(() => layoutSteps([{ ...base, id: 'x', depends_on: ['y'] }, { ...base, id: 'y', depends_on: ['x'] }])).not.toThrow();
  });
});

describe('WorkflowGraph', () => {
  it('shows role, agent, fan-out and the gate kind, and scrolls inside its own container', async () => {
    const { container } = render(<WorkflowGraph steps={template().definition!.steps} />);
    const region = screen.getByRole('region', { name: 'Template graph' });
    expect(region).toHaveClass('overflow-x-auto');
    expect(region).toHaveAttribute('tabindex', '0');
    expect(within(region).getByText('×3')).toBeInTheDocument();
    expect(within(region).getByText('Gate: trusted clean')).toBeInTheDocument();
    expect(within(region).getByText('Gate: approval security')).toBeInTheDocument();
    expect(within(region).getByText(/implementer · atlas/)).toBeInTheDocument();
    // The picture has a text alternative listing the steps in order.
    expect(screen.getByRole('img')).toHaveAccessibleName(/Implement one brief \(implementer, atlas, 3 in parallel\) after dispatch/);
    expect(container.querySelector('svg')?.getAttribute('width')).toBe(String(layoutSteps(template().definition!.steps).width));
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('TemplatesPanel', () => {
  const project = { kind: 'project', id: 'proj_1' } as const;

  it('lists templates, reveals a graph on demand and passes axe', async () => {
    const { container } = render(<TemplatesPanel target={project} tier="team" canEdit />);
    expect(screen.getByText('Ship a change')).toBeInTheDocument();
    expect(screen.getByText('Org template')).toBeInTheDocument();
    expect(screen.queryByTestId('workflow-graph')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Show graph for Ship a change/ }));
    expect(screen.getByRole('region', { name: 'Ship a change graph' })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('is gated below Team and never loads the list', () => {
    render(<TemplatesPanel target={project} tier="free" canEdit />);
    expect(screen.getByTestId('workflows-gated')).toBeInTheDocument();
    expect(hooks.useWorkflowTemplates).toHaveBeenCalledWith(project, false);
    expect(screen.queryByRole('button', { name: 'New template' })).not.toBeInTheDocument();
  });

  it('gives readers no write controls, and shows org templates read-only in a project', () => {
    render(<TemplatesPanel target={project} tier="team" canEdit={false} />);
    expect(screen.queryByRole('button', { name: 'New template' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Edit/ })).not.toBeInTheDocument();
    render(<TemplatesPanel target={project} tier="team" canEdit />);
    // Editors edit the project template but not the org one.
    expect(screen.getAllByRole('button', { name: /^Edit/ })).toHaveLength(1);
  });

  it('disables a template after a confirmation', async () => {
    const disable = mut();
    hooks.useDisableWorkflowTemplate.mockReturnValue(disable);
    render(<TemplatesPanel target={project} tier="enterprise" canEdit />);
    await userEvent.click(screen.getByRole('button', { name: /Disable Ship a change/ }));
    expect(disable.mutateAsync).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Disable template' }));
    expect(disable.mutateAsync).toHaveBeenCalledWith('wft_0123456789abcdef');
  });

  it('manages org templates for admins', async () => {
    hooks.useWorkflowTemplates.mockReturnValue(ok([orgTemplate()]));
    render(<TemplatesPanel target={{ kind: 'org', id: 'org_1' }} tier="enterprise" canEdit />);
    expect(screen.getByRole('button', { name: /Edit Org review pass/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Start/ })).not.toBeInTheDocument();
  });

  it('shows the empty state', () => {
    hooks.useWorkflowTemplates.mockReturnValue(ok([]));
    render(<TemplatesPanel target={project} tier="team" canEdit />);
    expect(screen.getByTestId('templates-empty')).toBeInTheDocument();
  });
});

describe('TemplateEditorDialog', () => {
  const target = { kind: 'project', id: 'proj_1' } as const;

  it('saves a new template parsed from the text', async () => {
    const save = mut();
    hooks.useSaveWorkflowTemplate.mockReturnValue(save);
    const onClose = vi.fn();
    render(<TemplateEditorDialog target={target} onClose={onClose} />);
    await userEvent.click(screen.getByRole('button', { name: 'Create template' }));
    const call = save.mutateAsync.mock.calls[0][0];
    expect(call.templateId).toBeUndefined();
    expect(call.body.name).toBe('Ship a change');
    expect((call.body.definition.steps as unknown[]).length).toBe(2);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('edits as a new version of the template', async () => {
    const save = mut();
    hooks.useSaveWorkflowTemplate.mockReturnValue(save);
    render(<TemplateEditorDialog target={target} template={template()} onClose={vi.fn()} />);
    expect(screen.getByText(/Saving creates version 3/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save new version' }));
    expect(save.mutateAsync.mock.calls[0][0].templateId).toBe('wft_0123456789abcdef');
  });

  it('shows a local parse error and does not call the server', async () => {
    const save = mut();
    hooks.useSaveWorkflowTemplate.mockReturnValue(save);
    render(<TemplateEditorDialog target={target} onClose={vi.fn()} />);
    const box = screen.getByLabelText('Template');
    await userEvent.clear(box);
    await userEvent.type(box, 'steps: x');
    await userEvent.click(screen.getByRole('button', { name: 'Create template' }));
    expect(screen.getByText(/needs a name/)).toBeInTheDocument();
    expect(save.mutateAsync).not.toHaveBeenCalled();
    expect(box).toHaveAttribute('aria-invalid', 'true');
  });

  it('shows the server validation error inline and stays open', async () => {
    const err = new ApiError(422, JSON.stringify({ detail: { error: 'invalid_workflow_definition', message: "steps.0.role: Input should be 'implementer'" } }));
    const save = mut({ mutateAsync: vi.fn().mockRejectedValue(err), error: err });
    hooks.useSaveWorkflowTemplate.mockReturnValue(save);
    const onClose = vi.fn();
    const { container } = render(<TemplateEditorDialog target={target} onClose={onClose} />);
    await userEvent.click(screen.getByRole('button', { name: 'Create template' }));
    expect(await screen.findByText(/steps\.0\.role/)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('locks the form while saving', () => {
    hooks.useSaveWorkflowTemplate.mockReturnValue(mut({ isPending: true }));
    render(<TemplateEditorDialog target={target} onClose={vi.fn()} />);
    expect(screen.getByLabelText('Template')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  });
});

describe('StartWorkflowDialog', () => {
  const tpls = [template(), orgTemplate()];

  it('starts from a fresh title with budgets', async () => {
    const start = mut({ mutateAsync: vi.fn().mockResolvedValue(started()) });
    hooks.useStartWorkflow.mockReturnValue(start);
    const onStarted = vi.fn();
    render(<StartWorkflowDialog projectId="proj_1" templates={tpls} onClose={vi.fn()} onStarted={onStarted} />);
    await userEvent.type(screen.getByLabelText('Title'), 'Spend tracking');
    await userEvent.type(screen.getByLabelText('Token limit'), '2000000');
    await userEvent.type(screen.getByLabelText('Review round limit'), '4');
    await userEvent.type(screen.getByLabelText('Cost limit (USD)'), '12.5');
    await userEvent.click(screen.getByRole('button', { name: 'Start workflow' }));
    expect(start.mutateAsync).toHaveBeenCalledWith({
      template_id: 'wft_0123456789abcdef',
      title: 'Spend tracking',
      token_budget: 2000000,
      max_review_rounds: 4,
      cost_budget_usd: 12.5,
    });
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(expect.objectContaining({ loop_id: 'tk_wfloop' })));
  });

  it('adopts an existing issue', async () => {
    const start = mut({ mutateAsync: vi.fn().mockResolvedValue(started()) });
    hooks.useStartWorkflow.mockReturnValue(start);
    render(<StartWorkflowDialog projectId="proj_1" templates={tpls} initialTemplateId="wft_org0000000000001" onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: /Start from/ }));
    await userEvent.click(screen.getByRole('option', { name: 'An existing issue' }));
    await userEvent.type(screen.getByLabelText('Issue ticket id'), 'tk_issue1');
    await userEvent.click(screen.getByRole('button', { name: 'Start workflow' }));
    expect(start.mutateAsync).toHaveBeenCalledWith({ template_id: 'wft_org0000000000001', ticket_id: 'tk_issue1' });
  });

  it('validates locally before sending', async () => {
    const start = mut();
    hooks.useStartWorkflow.mockReturnValue(start);
    render(<StartWorkflowDialog projectId="proj_1" templates={tpls} onClose={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('Token limit'), '0');
    await userEvent.click(screen.getByRole('button', { name: 'Start workflow' }));
    expect(screen.getByText('Give the workflow a title.')).toBeInTheDocument();
    expect(start.mutateAsync).not.toHaveBeenCalled();
  });

  it('is locked while submitting, shows a server error, and passes axe', async () => {
    hooks.useStartWorkflow.mockReturnValue(mut({ isPending: true }));
    const { container, rerender } = render(<StartWorkflowDialog projectId="proj_1" templates={tpls} onClose={vi.fn()} />);
    expect(screen.getByLabelText('Title')).toBeDisabled();
    expect(screen.getByLabelText('Token limit')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    hooks.useStartWorkflow.mockReturnValue(mut({ error: new ApiError(422, JSON.stringify({ detail: { error: 'workflow_agent_unknown', message: "No active agent named 'scout' is visible in this project." } })) }));
    rerender(<StartWorkflowDialog projectId="proj_1" templates={tpls} onClose={vi.fn()} />);
    expect(screen.getByText(/No active agent named 'scout'/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('WorkflowLanes and the Loop Board', () => {
  const renderLanes = () =>
    render(
      <MemoryRouter>
        <WorkflowLanes projectId="proj_1" loopId="tk_wfloop" title="Spend tracking" />
      </MemoryRouter>,
    );

  it('shows one lane per step with state, gate, reason and children', async () => {
    const { container } = renderLanes();
    for (const id of ['dispatch', 'implement', 'review', 'verify', 'document']) {
      expect(screen.getByTestId(`lane-${id}`)).toBeInTheDocument();
    }
    const review = screen.getByTestId('lane-review');
    expect(review).toHaveAttribute('data-state', 'blocked');
    expect(within(review).getByText(/not passed, No trusted VERIFIED-CLEAN/)).toBeInTheDocument();
    expect(within(review).getByText('Waiting for implement.')).toBeInTheDocument();
    expect(within(screen.getByTestId('lane-document')).getByText('Needs a person')).toBeInTheDocument();
    expect(screen.getByText(/ready: implement/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('highlights ready tickets and offers their brief', async () => {
    renderLanes();
    const lane = screen.getByTestId('lane-implement');
    const rows = within(within(lane).getByRole('list', { name: /Tickets for/ })).getAllByRole('listitem');
    expect(rows.map((r) => r.getAttribute('data-ready'))).toEqual(['true', 'true', 'false']);
    await userEvent.click(within(lane).getByText(/Brief for Implement 1\/3/));
    expect(within(lane).getByText(/Known traps/)).toBeInTheDocument();
  });

  it('shows loading and error states', () => {
    hooks.useWorkflowPlan.mockReturnValue({ data: undefined, isLoading: true, error: null });
    const { unmount } = renderLanes();
    expect(screen.getByText('Loading the plan…')).toBeInTheDocument();
    unmount();
    hooks.useWorkflowPlan.mockReturnValue({ data: undefined, isLoading: false, error: new ApiError(409, JSON.stringify({ detail: { message: "This loop's pinned template version can no longer be read." } })) });
    renderLanes();
    expect(screen.getByText(/can no longer be read/)).toBeInTheDocument();
  });

  it('LoopBoard uses lanes for a workflow loop and the usual lanes otherwise', () => {
    render(
      <MemoryRouter>
        <LoopBoard projectId="proj_1" loops={[workflowLoop(), ...loops(false)]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('workflow-lanes')).toBeInTheDocument();
    expect(hooks.useWorkflowPlan).toHaveBeenCalledTimes(1);
    // The plain loop still renders its per-ticket lanes.
    expect(screen.getByText('Record token usage per run')).toBeInTheDocument();
    // Plain loop children are not part of the workflow lanes.
    expect(screen.getAllByLabelText(/^Stages for /).length).toBeGreaterThan(0);
  });

  it('does not fetch a plan for loops without a workflow', () => {
    render(
      <MemoryRouter>
        <LoopBoard projectId="proj_1" loops={loops(false)} />
      </MemoryRouter>,
    );
    expect(hooks.useWorkflowPlan).not.toHaveBeenCalled();
  });
});
