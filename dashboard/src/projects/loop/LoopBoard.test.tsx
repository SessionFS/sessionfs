import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { LoopChild } from '../../api/reviewLoop';
import { LoopBoard } from './LoopBoard';
import ReviewLoopTab from './ReviewLoopTab';
import { designProblem, forecast, laneStages } from './boardModel';
import { checklist, insights, loops } from './fixtures';
import { axe } from '../../test/axe';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useLoops: vi.fn(),
    useLoopInsights: vi.fn(),
    useStory: vi.fn(),
    useTicketSpend: vi.fn(),
    useChecklist: vi.fn(),
    useUpdateLoop: vi.fn(),
    useCloseLoopItem: vi.fn(),
    useSeedChecklist: vi.fn(),
    usePatchChecklistItem: vi.fn(),
  },
}));
vi.mock('../../hooks/useReviewLoop', () => hooks);

const ok = (data: unknown) => ({ data, isLoading: false, error: null });
const idle = () => ({ mutate: vi.fn(), mutateAsync: vi.fn().mockResolvedValue({}), isPending: false, error: null });

const child = (over: Partial<LoopChild> = {}): LoopChild => ({ ...loops()[0].children[1], needs_human: null, ...over });

function setup(l = loops(), list = checklist()) {
  hooks.useLoops.mockReturnValue(ok(l));
  hooks.useLoopInsights.mockReturnValue(ok(insights()));
  hooks.useChecklist.mockReturnValue(ok(list));
  hooks.useUpdateLoop.mockReturnValue(idle());
  hooks.useCloseLoopItem.mockReturnValue(idle());
  hooks.useSeedChecklist.mockReturnValue(idle());
  hooks.usePatchChecklistItem.mockReturnValue(idle());
}
beforeEach(() => vi.clearAllMocks());

const renderBoard = (l = loops()) =>
  render(
    <MemoryRouter>
      <main>
        <LoopBoard projectId="proj_1" loops={l} />
      </main>
    </MemoryRouter>,
  );

describe('lane stages', () => {
  it('derives stages from rounds and marks the current one', () => {
    const { stages, current } = laneStages(child());
    expect(stages.map((s) => s.label)).toEqual([
      'Brief', 'Build', 'Review R1', 'Fix', 'Review R2', 'Fix', 'Clean', 'Ready', 'Merged',
    ]);
    expect(stages[current].label).toBe('Fix');
  });
  it('sits at Brief, Build, Ready and Merged for the matching children', () => {
    const at = (c: LoopChild) => laneStages(c).stages[laneStages(c).current].label;
    expect(at(child({ rounds: [], round: 0, status: 'open', harmful_open: 0 }))).toBe('Brief');
    expect(at(child({ rounds: [], round: 0, status: 'in_progress', harmful_open: 0 }))).toBe('Build');
    expect(at(loops()[0].children[0])).toBe('Ready');
    expect(at(child({ status: 'done' }))).toBe('Merged');
  });
  it('renders one lane per child with the current stage announced', () => {
    renderBoard();
    expect(screen.getAllByRole('list', { name: /Stages for/ })).toHaveLength(2);
    expect(screen.getAllByText('(current stage)')).toHaveLength(2);
  });
});

describe('rings', () => {
  it('labels each ring and grows it with harm; clean is empty', () => {
    renderBoard();
    const lane = screen.getByRole('list', { name: 'Stages for Record token usage per run' });
    const r1 = within(lane).getByRole('img', { name: 'Round 1: 3 harmful findings' });
    const r2 = within(lane).getByRole('img', { name: 'Round 2: 1 harmful finding' });
    const r3 = within(lane).getByRole('img', { name: 'Round 3: 0 harmful findings' });
    const radius = (el: HTMLElement) => Number(el.querySelector('circle')!.getAttribute('data-radius'));
    expect(radius(r1)).toBeGreaterThan(radius(r2));
    expect(radius(r2)).toBeGreaterThan(radius(r3));
    expect(r3.querySelector('circle')).toHaveAttribute('data-harmful', 'false');
    expect(r3.querySelector('circle')).toHaveAttribute('fill', 'none');
  });
});

describe('design problem hint', () => {
  const rounds = (...h: number[]) => h.map((harmful, i) => ({ round: i + 1, harmful }));
  it('flags three rounds that do not shrink', () => {
    expect(designProblem(child({ rounds: rounds(3, 3, 4) }))).toBe(true);
    expect(designProblem(child({ rounds: rounds(2, 2, 2) }))).toBe(true);
  });
  it('stays quiet for healthy, short or clean branches', () => {
    expect(designProblem(child({ rounds: rounds(3, 1, 0) }))).toBe(false);
    expect(designProblem(child({ rounds: rounds(4, 4) }))).toBe(false);
    expect(designProblem(child({ rounds: rounds(0, 0, 0) }))).toBe(false);
  });
  it('shows the hint on the lane', () => {
    renderBoard([{ ...loops()[0], children: [child({ rounds: rounds(3, 3, 4), round: 3 })] }]);
    expect(screen.getByText(/Design problem\?/)).toBeInTheDocument();
  });
});

describe('forecast', () => {
  it('projects tokens from the average per round and the budget room', () => {
    const c = child({ tokens: 300_000, round: 3, rounds: [{ round: 1, harmful: 2 }, { round: 2, harmful: 2 }, { round: 3, harmful: 1 }], harmful_open: 1, budget: { tokens: 400_000 }, cost_usd: undefined });
    expect(forecast(c)).toBe('At this rate, 2 more rounds ≈ 200k tokens; the budget allows 1.');
  });
  it('is hidden for clean or ready work', () => {
    expect(forecast(loops()[0].children[0])).toBeNull();
  });
  it('adds dollars only when cost_usd is present', () => {
    const withUsd = child({ tokens: 300_000, round: 3, harmful_open: 1, cost_usd: 3, budget: { tokens: 400_000 } });
    expect(forecast(withUsd)).toContain('$2.00');
    const { container } = renderBoard(loops(false));
    expect(container.textContent).not.toContain('$');
  });
});

describe('Board view and Needs you', () => {
  function renderTab(view: string | null, canEdit = true) {
    return render(
      <MemoryRouter>
        <main>
          <ReviewLoopTab projectId="proj_1" storyId={null} onOpenStory={vi.fn()} canEdit={canEdit} view={view} onChangeView={vi.fn()} />
        </main>
      </MemoryRouter>,
    );
  }
  it('switches to the board from the tab and reports the view change', async () => {
    setup();
    const onChangeView = vi.fn();
    render(
      <MemoryRouter>
        <main>
          <ReviewLoopTab projectId="proj_1" storyId={null} onOpenStory={vi.fn()} view={null} onChangeView={onChangeView} />
        </main>
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByRole('tab', { name: 'Board' }));
    expect(onChangeView).toHaveBeenCalledWith('board');
  });
  it('renders the board when view=board', () => {
    setup();
    renderTab('board');
    expect(screen.getByRole('region', { name: 'Loop board' })).toBeInTheDocument();
  });
  it('lists merge-ready, parked and proposed items', () => {
    setup();
    renderTab(null);
    const region = screen.getByRole('region', { name: /Needs you/ });
    expect(within(region).getByRole('link', { name: /Review and merge/ })).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: /Raise limit/ })).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: /Close item/ })).toBeInTheDocument();
    expect(within(region).getByText(/Proposed checklist item/)).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: /^Accept/ })).toBeInTheDocument();
  });
  it('accepts a proposed item', async () => {
    setup();
    const patch = idle();
    hooks.usePatchChecklistItem.mockReturnValue(patch);
    renderTab(null);
    const region = screen.getByRole('region', { name: /Needs you/ });
    await userEvent.click(within(region).getByRole('button', { name: /^Accept/ }));
    expect(patch.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ status: 'active' }));
  });
  it('is hidden when empty', () => {
    const l = loops();
    l[0].children = l[0].children.map((c) => ({ ...c, ready: false, needs_human: null }));
    setup(l, checklist().map((i) => ({ ...i, status: 'active' as const })));
    renderTab(null);
    expect(screen.queryByRole('region', { name: /Needs you/ })).not.toBeInTheDocument();
  });
  it('has no axe violations on the board', async () => {
    setup();
    const { container } = renderTab('board');
    expect(await axe(container)).toHaveNoViolations();
  });
});
