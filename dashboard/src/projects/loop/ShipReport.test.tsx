import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ReviewLoopTab from './ReviewLoopTab';
import { Scoreboard } from './Panels';
import { ShipReports, currentMonth, shipHeadline } from './ShipReport';
import { checklist, insights, loops, releaseList, scoreboard, shipReport, story } from './fixtures';
import { axe } from '../../test/axe';

const hooks = vi.hoisted(() => ({
  useLoops: vi.fn(),
  useLoopInsights: vi.fn(),
  useStory: vi.fn(),
  useTicketSpend: vi.fn(),
  useChecklist: vi.fn(),
  useUpdateLoop: vi.fn(),
  useCloseLoopItem: vi.fn(),
  useSeedChecklist: vi.fn(),
  usePatchChecklistItem: vi.fn(),
  useReleases: vi.fn(),
  useShipReport: vi.fn(),
}));
vi.mock('../../hooks/useReviewLoop', () => hooks);

const ok = (data: unknown) => ({ data, isLoading: false, error: null });
const idle = () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false, error: null });

function setup(opts: { usd?: boolean; releases?: boolean } = {}) {
  const usd = opts.usd ?? true;
  hooks.useLoops.mockReturnValue(ok(loops(usd)));
  hooks.useLoopInsights.mockReturnValue(ok(insights(usd)));
  hooks.useStory.mockReturnValue(ok(story()));
  hooks.useChecklist.mockReturnValue(ok(checklist()));
  hooks.useUpdateLoop.mockReturnValue(idle());
  hooks.useCloseLoopItem.mockReturnValue(idle());
  hooks.useSeedChecklist.mockReturnValue(idle());
  hooks.usePatchChecklistItem.mockReturnValue(idle());
  hooks.useTicketSpend.mockReturnValue(
    ok({ totals: { runs: 1, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0, tokens_on_plans: 0 }, per_persona: [], per_model: [], review_rounds: 1 }),
  );
  hooks.useReleases.mockReturnValue(ok(opts.releases === false ? { project_id: 'proj_1', releases: [] } : releaseList()));
  hooks.useShipReport.mockReturnValue(ok(shipReport(usd)));
}

beforeEach(() => vi.clearAllMocks());

describe('Ship Reports', () => {
  it('shows the headline and numbers for the newest release', () => {
    setup();
    render(<ShipReports projectId="proj_1" />);
    expect(hooks.useShipReport).toHaveBeenCalledWith('proj_1', 'v0.29.0');
    expect(screen.getByTestId('ship-headline')).toHaveTextContent(
      'v0.29.0 — 4 loops, 14 review rounds, 63 findings, 19 harmful, all fixed before merge',
    );
    expect(screen.getByText('Data exposed: 7')).toBeInTheDocument();
    expect(screen.getByText('Access rechecked after a slow step')).toBeInTheDocument();
    expect(screen.getByTestId('ship-gates')).toHaveTextContent('2 passed, 1 failed, 1 skipped');
    expect(screen.getByText(/production/)).toBeInTheDocument();
  });

  it('states when harm is still open', () => {
    expect(shipHeadline({ ...shipReport(), all_fixed_before_merge: false })).toMatch(/some still open$/);
  });

  it('shows dollars only when the API returns usd', () => {
    setup({ usd: true });
    const { unmount } = render(<ShipReports projectId="proj_1" />);
    expect(screen.getAllByText(/\$0\.41/).length).toBeGreaterThan(0);
    unmount();
    setup({ usd: false });
    render(<ShipReports projectId="proj_1" />);
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/310k tokens/).length).toBeGreaterThan(0);
  });

  it('links only safe PR urls, with noopener', () => {
    setup();
    render(<ShipReports projectId="proj_1" />);
    const link = screen.getByRole('link', { name: /acme\/app #12/ });
    expect(link).toHaveAttribute('href', 'https://github.com/acme/app/pull/12');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByRole('link', { name: /Bad link/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Bad link/)).toBeInTheDocument();
  });

  it('copies the API markdown', async () => {
    setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<ShipReports projectId="proj_1" />);
    await userEvent.click(screen.getByRole('button', { name: 'Copy markdown' }));
    expect(writeText).toHaveBeenCalledWith(shipReport().markdown);
    expect(await screen.findByText('Markdown copied.')).toBeInTheDocument();
  });

  it('offers a This month option that requests the month report', async () => {
    setup();
    render(<ShipReports projectId="proj_1" />);
    await userEvent.click(screen.getByRole('button', { name: /Release/ }));
    await userEvent.click(screen.getByRole('option', { name: 'This month' }));
    expect(hooks.useShipReport).toHaveBeenLastCalledWith('proj_1', `month:${currentMonth()}`);
  });

  it('explains sfs release record when there are no releases', () => {
    setup({ releases: false });
    render(<ShipReports projectId="proj_1" />);
    expect(screen.getByTestId('ship-empty')).toHaveTextContent('sfs release record');
    expect(screen.queryByRole('button', { name: 'Copy markdown' })).not.toBeInTheDocument();
  });

  it('is the third tab and has no axe violations', async () => {
    setup();
    const { container } = render(
      <MemoryRouter>
        <main>
          <ReviewLoopTab projectId="proj_1" storyId={null} onOpenStory={vi.fn()} view="ship" />
        </main>
      </MemoryRouter>,
    );
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Overview', 'Board', 'Ship Reports']);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Model scoreboard', () => {
  it('renders pairings with dollars when present', async () => {
    const { container } = render(<Scoreboard rows={scoreboard()} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText(/deepseek-v4/)).toBeInTheDocument();
    expect(within(table).getByText('1.5')).toBeInTheDocument();
    expect(within(table).getByText('2.5')).toBeInTheDocument();
    expect(within(table).getByText('$0.41')).toBeInTheDocument();
    expect(within(table).getByText('Not yet')).toBeInTheDocument();
    expect(within(table).getByText(/Unknown model/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('falls back to tokens without usd', () => {
    render(<Scoreboard rows={scoreboard(false)} />);
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.getByText('310k tokens')).toBeInTheDocument();
  });

  it('is hidden when empty, and shown in the Overview when present', () => {
    const { container } = render(<Scoreboard rows={[]} />);
    expect(container).toBeEmptyDOMElement();
    setup();
    render(
      <MemoryRouter>
        <main><ReviewLoopTab projectId="proj_1" storyId={null} onOpenStory={vi.fn()} /></main>
      </MemoryRouter>,
    );
    expect(screen.getByRole('heading', { name: 'Model scoreboard' })).toBeInTheDocument();
  });
});

describe('Story shipped context', () => {
  it('lists PRs, releases and deployments', () => {
    setup();
    render(
      <MemoryRouter>
        <main><ReviewLoopTab projectId="proj_1" storyId="tk_child1" onOpenStory={vi.fn()} /></main>
      </MemoryRouter>,
    );
    const box = screen.getByTestId('story-shipped');
    expect(within(box).getByRole('link', { name: /acme\/app #12/ })).toHaveAttribute('rel', 'noopener noreferrer');
    expect(within(box).getByText('v0.29.0')).toBeInTheDocument();
    expect(within(box).getByText(/production/)).toBeInTheDocument();
  });

  it('is absent when the story has none', () => {
    setup();
    hooks.useStory.mockReturnValue(ok({ ...story(), prs: undefined, releases: undefined, deployments: undefined }));
    render(
      <MemoryRouter>
        <main><ReviewLoopTab projectId="proj_1" storyId="tk_child1" onOpenStory={vi.fn()} /></main>
      </MemoryRouter>,
    );
    expect(screen.queryByTestId('story-shipped')).not.toBeInTheDocument();
  });
});
