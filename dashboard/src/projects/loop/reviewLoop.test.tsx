import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ReviewLoopTab from './ReviewLoopTab';
import { TrustSeal } from './TrustSeal';
import { BudgetBar, CostTrend } from './Panels';
import { checklist, insights, loops, story } from './fixtures';
import { axe } from '../../test/axe';
import { createReviewLoopApi } from '../../api/reviewLoop';

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
const idle = () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false, error: null });

function setup(opts: { usd?: boolean; empty?: boolean } = {}) {
  const usd = opts.usd ?? true;
  hooks.useLoops.mockReturnValue(ok(opts.empty ? [] : loops(usd)));
  hooks.useLoopInsights.mockReturnValue(
    ok(opts.empty ? { caught: [], cost_per_accepted_change: [], learning: [] } : insights(usd)),
  );
  hooks.useStory.mockReturnValue(ok(story()));
  hooks.useChecklist.mockReturnValue(ok(opts.empty ? [] : checklist()));
  hooks.useUpdateLoop.mockReturnValue(idle());
  hooks.useCloseLoopItem.mockReturnValue(idle());
  hooks.useSeedChecklist.mockReturnValue(idle());
  hooks.usePatchChecklistItem.mockReturnValue(idle());
  hooks.useTicketSpend.mockReturnValue(
    ok({ totals: { runs: 3, input_tokens: 100_000, output_tokens: 20_000, cache_read_tokens: 0, cache_write_tokens: 0, tokens_on_plans: 0, ...(usd ? { cost_usd: 1.4 } : {}) }, per_persona: [], per_model: [], review_rounds: 3 }),
  );
}

function renderTab(storyId: string | null = null, onOpenStory = vi.fn()) {
  return render(
    <MemoryRouter>
      <main>
        <ReviewLoopTab projectId="proj_1" storyId={storyId} onOpenStory={onOpenStory} />
      </main>
    </MemoryRouter>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('Mission Control', () => {
  it('lists what needs you with one action each', () => {
    setup();
    renderTab();
    const region = screen.getByRole('region', { name: /Needs you/ });
    expect(within(region).getByRole('link', { name: /Review and merge/ })).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=loop&story=tk_child1',
    );
    expect(within(region).getByText('token budget hit (410,000 tokens spent)')).toBeInTheDocument();
    expect(within(region).getAllByRole('link')).toHaveLength(2);
  });

  it('hides Needs you when nothing waits on a person', () => {
    setup();
    const l = loops();
    l[0].children = l[0].children.map((c) => ({ ...c, ready: false, needs_human: null }));
    hooks.useLoops.mockReturnValue(ok(l));
    renderTab();
    expect(screen.queryByText('Needs you')).not.toBeInTheDocument();
  });

  it('shows only harmful catches, counted from data, with run and fix links', () => {
    setup();
    renderTab();
    const caught = screen.getByRole('region', { name: 'What the loop caught' });
    expect(within(caught).getByText('2 harmful findings')).toBeInTheDocument();
    expect(within(caught).getByText(/1 data exposed, 1 money spent/)).toBeInTheDocument();
    expect(within(caught).queryByText('Naming nit')).not.toBeInTheDocument();
    expect(within(caught).getByText('src/routes/keys.py:88')).toBeInTheDocument();
    expect(within(caught).getByText('abc1234')).toBeInTheDocument();
    expect(within(caught).getByText('Not fixed yet')).toBeInTheDocument();
    expect(
      within(caught).getByRole('link', { name: /Run for Org key readable/ }),
    ).toHaveAttribute('href', '/projects/proj_1?tab=work&sub=agent-runs&run=run_a1');
  });

  it('keeps duplicate findings in one run as separate cards', () => {
    setup();
    const ins = insights();
    ins.caught = [ins.caught[0], { ...ins.caught[0] }];
    hooks.useLoopInsights.mockReturnValue(ok(ins));
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderTab();
    expect(screen.getAllByText('Org key readable by members')).toHaveLength(2);
    expect(err.mock.calls.flat().join(' ')).not.toMatch(/same key/);
    err.mockRestore();
  });

  it('uses the danger colour only for harm', () => {
    setup();
    const { container } = renderTab();
    const loud = [...container.querySelectorAll('.text-danger')].map((e) => e.textContent);
    // Headline count and the harmful-open line of the one card that has some.
    expect(loud).toEqual(['2 harmful findings', '2 harmful findings open']);
    const rings = [...container.querySelectorAll('circle[data-harmful]')];
    expect(rings.filter((r) => r.getAttribute('data-harmful') === 'true')).toHaveLength(4); // rounds 3 and 1, then 4 and 2
  });

  it('shows dollars only when cost_usd is present', () => {
    setup({ usd: true });
    const { unmount } = renderTab();
    expect(screen.getAllByText('$0.38', { exact: false }).length).toBeGreaterThan(0);
    unmount();

    setup({ usd: false });
    const { container } = renderTab();
    expect(container.textContent).not.toMatch(/\$\d/);
    expect(screen.getByText(/250k tokens/)).toBeInTheDocument();
    expect(screen.getByText(/240k tokens of 400k tokens/)).toBeInTheDocument();
  });

  it('marks when each class joined the checklist, with counts before and since', () => {
    setup();
    renderTab();
    const learning = screen.getByRole('region', { name: 'Your loop is learning' });
    expect(within(learning).getByText('Access rechecked after a slow step')).toBeInTheDocument();
    expect(within(learning).getByText(/6 times found before the checklist, 1 since/)).toBeInTheDocument();
    expect(within(learning).getByTestId('join-marker')).toBeInTheDocument();
  });

  it('draws one ring per round with an accessible description', () => {
    setup();
    renderTab();
    expect(
      screen.getByRole('img', { name: 'Review rounds, round 1: 3 harmful findings, round 2: 1 harmful finding, round 3: 0 harmful findings' }),
    ).toBeInTheDocument();
  });

  it('has a budget progress bar with a text value', () => {
    setup();
    renderTab();
    const bars = screen.getAllByRole('progressbar', { name: 'Budget used' });
    expect(bars[0]).toHaveAttribute('aria-valuenow', '60');
    expect(bars[1]).toHaveAttribute('aria-valuenow', '100');
    expect(screen.getByText(/budget reached/)).toBeInTheDocument();
  });

  it('explains the Review Loop when there are none', () => {
    setup({ empty: true });
    renderTab();
    const empty = screen.getByTestId('loop-empty');
    expect(within(empty).getByText(/sfs loop start/)).toBeInTheDocument();
    expect(within(empty).getByText(/coming soon/)).toBeInTheDocument();
    expect(within(empty).getByRole('link', { name: /Read how it works/ })).toHaveAttribute(
      'href',
      expect.stringContaining('sessionfs.dev'),
    );
    expect(screen.queryByText('Needs you')).not.toBeInTheDocument();
  });

  it('shows a quiet message per section when only some data exists', () => {
    setup();
    hooks.useLoopInsights.mockReturnValue(ok({ caught: [], cost_per_accepted_change: [], learning: [] }));
    renderTab();
    expect(screen.getByText(/No harmful findings recorded in this period/)).toBeInTheDocument();
    expect(screen.getByText(/Appears once a change has been reviewed clean/)).toBeInTheDocument();
    expect(screen.getByText(/Recurring kinds of finding become checklist items/)).toBeInTheDocument();
  });

  it('shows an error when the loops fail to load', () => {
    setup();
    hooks.useLoops.mockReturnValue({ data: undefined, isLoading: false, error: new Error('boom') });
    renderTab();
    expect(screen.getByText(/Could not load Review Loop: boom/)).toBeInTheDocument();
  });

  it('has no axe violations', async () => {
    setup();
    const { container } = renderTab();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('has no axe violations when empty', async () => {
    setup({ empty: true });
    const { container } = renderTab();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Trust seal', () => {
  const verdict = { reviewer: 'codex-reviewer', reviewed_commit: '3f9a2c1d44aa', at: '2026-10-29T10:00:00Z' };

  it('shows reviewer, short commit and time, and opens the verdict', async () => {
    render(<TrustSeal readiness={{ ready: true, reasons: [], verdict }} />);
    const btn = screen.getByRole('button', { name: /codex-reviewer/ });
    expect(btn).toHaveTextContent('3f9a2c1');
    await userEvent.click(btn);
    const dialog = screen.getByRole('dialog', { name: 'Verdict' });
    expect(within(dialog).getByText('codex-reviewer')).toBeInTheDocument();
    expect(within(dialog).getByTitle('3f9a2c1d44aa')).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('says why when not ready', () => {
    render(<TrustSeal readiness={{ ready: false, reasons: ['No trusted verdict', '1 harmful finding open'], verdict: null }} />);
    expect(screen.getByText('Not ready')).toBeInTheDocument();
    expect(screen.getByText('No trusted verdict')).toBeInTheDocument();
    expect(screen.getByText('1 harmful finding open')).toBeInTheDocument();
  });

  it('collapses reasons when compact', () => {
    render(<TrustSeal compact readiness={{ ready: false, reasons: ['a', 'b', 'c'], verdict: null }} />);
    expect(screen.getByText('a')).toBeInTheDocument();
    expect(screen.queryByText('b')).not.toBeInTheDocument();
    expect(screen.getByText('and 2 more')).toBeInTheDocument();
  });
});

describe('Cost and budget units', () => {
  it('shows the dollar cap when it is the closest one, and never $0 for absent dollars', () => {
    const c = loops()[0].children[0];
    const { unmount } = render(<BudgetBar child={{ ...c, budget: { cost_usd: 2 }, cost_usd: 1.4 }} />);
    expect(screen.getByText('$1.40 of $2.00')).toBeInTheDocument();
    unmount();
    const { cost_usd: _drop, ...noUsd } = c;
    render(<BudgetBar child={noUsd} />);
    expect(document.body.textContent).not.toMatch(/\$/);
  });
  it('falls back to tokens when any point lacks dollars', () => {
    render(<CostTrend points={[{ period: 'Sep', tokens: 1000, cost_usd: 2 }, { period: 'Oct', tokens: 500 }]} />);
    expect(document.body.textContent).not.toMatch(/\$/);
  });
  it('budget bar without a cap reads as plain spend', () => {
    const c = loops()[0].children[0];
    render(<BudgetBar child={{ ...c, budget: {}, cost_usd: undefined }} />);
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.getByText(/240k tokens spent/)).toBeInTheDocument();
  });
});

describe('Story', () => {
  it('renders the timeline in order with the seal at the merge point', async () => {
    setup();
    const onOpenStory = vi.fn();
    renderTab('tk_child1', onOpenStory);
    const list = screen.getByRole('list', { name: 'Story of this change' });
    const kinds = within(list).getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(kinds[0]).toMatch(/Brief/);
    expect(kinds[kinds.length - 1]).toMatch(/Merged/);
    const merge = within(list).getByText('Merged', { selector: 'p' }).closest('li') as HTMLElement;
    expect(within(merge).getByRole('button', { name: /codex-reviewer/ })).toBeInTheDocument();
    expect(within(list).getAllByRole('link', { name: 'Run record' })).toHaveLength(2);
    expect(screen.getByText(/3 review rounds/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'All loops' }));
    expect(onOpenStory).toHaveBeenCalledWith(null);
  });

  it('is loud only for the harmful finding', () => {
    setup();
    const { container } = renderTab('tk_child1');
    expect(container.querySelectorAll('[data-harm="true"]')).toHaveLength(1);
  });

  it('renders an event with no timestamp as "Time not recorded" and sorts it first', () => {
    setup();
    const s = story();
    s.events = [
      { kind: 'brief', at: '2026-10-28T08:00:00Z', title: 'Brief' },
      { kind: 'merge', at: null, title: 'Merged' },
    ];
    hooks.useStory.mockReturnValue(ok(s));
    renderTab('tk_child1');
    const items = within(screen.getByRole('list', { name: 'Story of this change' })).getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('Time not recorded');
  });

  it('only links http(s) urls', () => {
    setup();
    const s = story();
    s.events = [{ kind: 'pr', at: '2026-10-29T10:30:00Z', title: 'PR', url: 'javascript:alert(1)' }];
    hooks.useStory.mockReturnValue(ok(s));
    renderTab('tk_child1');
    expect(screen.queryByRole('link', { name: /Open/ })).not.toBeInTheDocument();
  });

  it('shows the not-ready reasons at the merge point before a merge', () => {
    setup();
    const s = story();
    s.events = s.events.filter((e) => e.kind !== 'merge');
    hooks.useStory.mockReturnValue(ok(s));
    const l = loops();
    l[0].children[0] = { ...l[0].children[0], ready: false, verdict: null, not_ready_reasons: ['No trusted verdict on this commit'] };
    hooks.useLoops.mockReturnValue(ok(l));
    renderTab('tk_child1');
    expect(screen.getByText('Merge point')).toBeInTheDocument();
    expect(screen.getByText('No trusted verdict on this commit')).toBeInTheDocument();
  });

  it('has no axe violations', async () => {
    setup();
    const { container } = renderTab('tk_child1');
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('API layer', () => {
  it('builds the documented urls', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal('fetch', fetchMock);
    const api = createReviewLoopApi('https://x.test', 'k');
    await api.listLoops('proj 1');
    await api.getInsights('p', { from: '2026-10-01', to: '2026-10-31' });
    await api.getSpend('p', 'role');
    await api.getReadiness('p', 'tk_1', 'abc');
    await api.getStory('p', 'tk_1');
    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toEqual([
      'https://x.test/api/v1/projects/proj%201/loops',
      'https://x.test/api/v1/projects/p/loop-insights?from=2026-10-01&to=2026-10-31',
      'https://x.test/api/v1/projects/p/spend?group_by=role',
      'https://x.test/api/v1/projects/p/tickets/tk_1/readiness?sha=abc',
      'https://x.test/api/v1/projects/p/tickets/tk_1/story',
    ]);
    vi.unstubAllGlobals();
  });
});
