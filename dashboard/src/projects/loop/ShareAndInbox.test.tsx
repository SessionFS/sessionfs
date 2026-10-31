import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import ReviewLoopsPage from './ReviewLoopsPage';
import PublicLoopShare from './PublicLoopShare';
import { ShareLinkDialog } from './ShareLinkDialog';
import { StoryView } from './StoryView';
import { ShipReports } from './ShipReport';
import { loops, needsYou, publicShipReport, releaseList, shareLinks, shipReport, story } from './fixtures';
import { axe } from '../../test/axe';

const hooks = vi.hoisted(() => ({
  useNeedsYouItems: vi.fn(),
  useShareLinks: vi.fn(),
  useCreateShareLink: vi.fn(),
  useRevokeShareLink: vi.fn(),
  useStory: vi.fn(),
  useTicketSpend: vi.fn(),
  useReleases: vi.fn(),
  useShipReport: vi.fn(),
}));
vi.mock('../../hooks/useReviewLoop', () => hooks);

const ok = (data: unknown) => ({ data, isLoading: false, error: null });

function mutation(fn = vi.fn()) {
  return { mutateAsync: fn, mutate: fn, isPending: false, error: null };
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useNeedsYouItems.mockReturnValue(ok(needsYou()));
  hooks.useShareLinks.mockReturnValue(ok(shareLinks()));
  hooks.useCreateShareLink.mockReturnValue(mutation());
  hooks.useRevokeShareLink.mockReturnValue(mutation());
  hooks.useStory.mockReturnValue(ok(story()));
  hooks.useTicketSpend.mockReturnValue(ok(undefined));
  hooks.useReleases.mockReturnValue(ok(releaseList()));
  hooks.useShipReport.mockReturnValue(ok(shipReport()));
});

afterEach(() => vi.unstubAllGlobals());

describe('Review loops page', () => {
  it('groups items by project and links into the Review Loop tab and story', async () => {
    const { container } = render(
      <MemoryRouter>
        <ReviewLoopsPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole('heading', { level: 1, name: 'Review loops' })).toBeInTheDocument();
    const acme = screen.getByRole('region', { name: /Acme Widgets/ });
    const billing = screen.getByRole('region', { name: /Billing Service/ });
    expect(within(acme).getAllByRole('listitem')).toHaveLength(2);
    expect(within(billing).getAllByRole('listitem')).toHaveLength(1);
    expect(within(acme).getByRole('link', { name: 'Acme Widgets' })).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=loop',
    );
    expect(within(acme).getByRole('link', { name: 'Record token usage per run' })).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=loop&story=tk_child1',
    );
    expect(within(billing).getByRole('link', { name: 'Recheck access after any slow step' })).toHaveAttribute(
      'href',
      '/projects/proj_2?tab=work&sub=loop',
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('renders view-only items without any action button', () => {
    render(
      <MemoryRouter>
        <ReviewLoopsPage />
      </MemoryRouter>,
    );
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    const row = screen.getByText('Token budget hit (410,000 tokens spent)').closest('li')!;
    expect(within(row).getByText('View only')).toBeInTheDocument();
    const actionable = screen.getByText(/cleared commit/).closest('li')!;
    expect(within(actionable).queryByText('View only')).not.toBeInTheDocument();
  });

  it('shows an empty state', async () => {
    hooks.useNeedsYouItems.mockReturnValue(ok({ items: [], counts: { ready: 0, needs_human: 0, proposed_checklist: 0, total: 0 } }));
    const { container } = render(
      <MemoryRouter>
        <ReviewLoopsPage />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('loops-inbox-empty')).toHaveTextContent('Nothing needs you');
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Share dialog', () => {
  it('creates a link with the chosen expiry, shows it once and copies it', async () => {
    const create = vi.fn().mockResolvedValue({
      id: 'lsl_9',
      kind: 'story',
      ticket_id: 'tk_child1',
      release_version: null,
      month: null,
      token: 'tok',
      url: 'https://app.example/share/loop/tok',
      expires_at: '2036-11-27T10:00:00Z',
    });
    hooks.useCreateShareLink.mockReturnValue(mutation(create));
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<ShareLinkDialog projectId="proj_1" target={{ kind: 'story', ticketId: 'tk_child1' }} onClose={vi.fn()} />);

    expect(screen.getByText(/no dollars and no emails/)).toBeInTheDocument();
    expect(screen.getByText(/until it expires or you revoke it/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Link expires after/ }));
    await userEvent.click(screen.getByRole('option', { name: '90 days' }));
    await userEvent.click(screen.getByRole('button', { name: 'Create link' }));
    expect(create).toHaveBeenCalledWith({ target: { kind: 'story', ticketId: 'tk_child1' }, expiresDays: 90 });
    expect(await screen.findByDisplayValue('https://app.example/share/loop/tok')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Copy link' }));
    expect(writeText).toHaveBeenCalledWith('https://app.example/share/loop/tok');
    expect(await screen.findByText('Link copied.')).toBeInTheDocument();
  });

  it('defaults to 30 days', async () => {
    const create = vi.fn().mockResolvedValue({ id: 'x', kind: 'ship_report', token: 't', url: 'u', expires_at: null });
    hooks.useCreateShareLink.mockReturnValue(mutation(create));
    render(<ShareLinkDialog projectId="proj_1" target={{ kind: 'ship_report', releaseVersion: 'v0.29.0' }} onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Create link' }));
    expect(create).toHaveBeenCalledWith({ target: { kind: 'ship_report', releaseVersion: 'v0.29.0' }, expiresDays: 30 });
  });

  it('asks before revoking and only then revokes', async () => {
    const revoke = vi.fn().mockResolvedValue(undefined);
    hooks.useRevokeShareLink.mockReturnValue(mutation(revoke));
    render(<ShareLinkDialog projectId="proj_1" target={{ kind: 'story', ticketId: 'tk_child1' }} onClose={vi.fn()} />);
    const list = screen.getByRole('list', { name: 'Existing share links' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    await userEvent.click(within(list).getByRole('button', { name: /Revoke Story tk_child1/ }));
    expect(revoke).not.toHaveBeenCalled();
    const confirm = screen.getByRole('dialog', { name: 'Revoke this link?' });
    await userEvent.click(within(confirm).getByRole('button', { name: 'Keep link' }));
    expect(revoke).not.toHaveBeenCalled();
    await userEvent.click(within(list).getByRole('button', { name: /Revoke Story tk_child1/ }));
    await userEvent.click(within(screen.getByRole('dialog', { name: 'Revoke this link?' })).getByRole('button', { name: 'Revoke link' }));
    expect(revoke).toHaveBeenCalledWith('lsl_1');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Revoke this link?' })).not.toBeInTheDocument());
  });

  it('is offered to editors only', () => {
    const { rerender } = render(
      <MemoryRouter>
        <StoryView projectId="proj_1" ticketId="tk_child1" loops={loops()} onBack={vi.fn()} canEdit />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: 'Share' })).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <StoryView projectId="proj_1" ticketId="tk_child1" loops={loops()} onBack={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();
  });

  it('opens from a ship report for editors', async () => {
    render(<ShipReports projectId="proj_1" canEdit />);
    await userEvent.click(screen.getByRole('button', { name: 'Share' }));
    expect(screen.getByRole('dialog', { name: 'Share' })).toBeInTheDocument();
    cleanup();
    render(<ShipReports projectId="proj_1" />);
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();
  });
});

function renderPublic(token = 'tok123') {
  return render(
    <MemoryRouter initialEntries={[`/share/loop/${token}`]}>
      <Routes>
        <Route path="/share/loop/:token" element={<PublicLoopShare />} />
      </Routes>
    </MemoryRouter>,
  );
}

function stubFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

describe('Public loop share page', () => {
  it('renders a story read-only, sends no auth header and sets noindex', async () => {
    const fetchFn = stubFetch(200, story());
    const { container, unmount } = renderPublic();
    expect(await screen.findByRole('heading', { name: 'Record token usage per run' })).toBeInTheDocument();
    const [url, init] = fetchFn.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/share\/loop\/tok123$/);
    expect(JSON.stringify(init ?? {})).not.toMatch(/authorization/i);
    expect(init.credentials).toBe('omit');
    expect(document.head.querySelector('meta[name="robots"][content="noindex"]')).not.toBeNull();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Run record' })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
    unmount();
    expect(document.head.querySelector('meta[name="robots"]')).toBeNull();
  });

  it('renders a ship report with no dollars and a markdown copy', async () => {
    stubFetch(200, publicShipReport());
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const { container } = renderPublic();
    expect(await screen.findByTestId('ship-headline')).toHaveTextContent('v0.29.0');
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Download image|Share/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Copy markdown' }));
    expect(writeText).toHaveBeenCalledWith(publicShipReport().markdown);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('shows a neutral unavailable state on 404', async () => {
    stubFetch(404, { detail: 'Share link not found' });
    const { container } = renderPublic();
    expect(await screen.findByTestId('share-unavailable')).toHaveTextContent('This link is unavailable');
    expect(screen.queryByText(/not found/i)).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});
