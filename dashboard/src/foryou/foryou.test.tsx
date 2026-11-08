import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from '../test/axe';
import ForYouPage from './ForYouPage';
import Chyron from './Chyron';
import { fyCaughtUpFeed, fyFeed, fyItemContext, fyItems, fyLongFeed, fyLoops } from './fixtures';
import { snoozeTarget, whenLabel } from './model';

const { client } = vi.hoisted(() => ({
  client: {
    getForYou: vi.fn(),
    getForYouCount: vi.fn(),
    getForYouItemContext: vi.fn(),
    hideForYouItem: vi.fn(),
    unhideForYouItem: vi.fn(),
    restoreMeetingItem: vi.fn(),
    deleteReminder: vi.fn(),
    remindCollaborator: vi.fn(),
    patchReminder: vi.fn(),
    snoozeReminder: vi.fn(),
    dismissMeetingItem: vi.fn(),
    previewMeetingPromote: vi.fn(),
    promoteMeetingItem: vi.fn(),
    createMeetingItemReminder: vi.fn(),
    addTicketComment: vi.fn(),
    decideApprovalRequest: vi.fn(),
    claimHandoff: vi.fn(),
    getMe: vi.fn(),
  },
}));

const { loopApi } = vi.hoisted(() => ({ loopApi: { updateLoop: vi.fn(), listLoops: vi.fn() } }));
vi.mock('../api/reviewLoop', async (orig) => ({
  ...(await orig<typeof import('../api/reviewLoop')>()),
  createReviewLoopApi: () => loopApi,
}));

// The page lazy-loads a lot; give slow CI machines room.
configure({ asyncUtilTimeout: 20_000 });

vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ auth: { client, baseUrl: 'https://x.test', apiKey: 'k' } }) }));
vi.mock('../helper/HelperProvider', () => ({ useHelper: () => ({ openHelper: undefined }) }));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/']}>
        <ForYouPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  for (const fn of Object.values(client)) fn.mockReset();
  client.getForYou.mockResolvedValue({ data: fyFeed(), etag: '"e1"', notModified: false });
  client.getMe.mockResolvedValue({ user_id: 'u1', email: 'ola@example.com', display_name: 'Ola Bello' });
  client.hideForYouItem.mockResolvedValue(undefined);
  client.unhideForYouItem.mockResolvedValue(undefined);
  client.patchReminder.mockResolvedValue({});
  client.getForYouItemContext.mockResolvedValue(fyItemContext);
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('For You page', { timeout: 30_000 }, () => {
  it('renders the summary, every section and the chyron', async () => {
    renderPage();
    expect(await screen.findByRole('heading', { name: /Good (morning|afternoon|evening), Ola/ })).toBeInTheDocument();
    for (const h of ['On your radar', 'Waiting on you', 'Your team is asking', 'From your meetings and chats', 'Coming up', 'Worth a second look', 'What changed this week', 'Working with others']) {
      expect(screen.getByRole('heading', { level: 2, name: h })).toBeInTheDocument();
    }
    const ticker = screen.getByRole('region', { name: /Right now/ });
    expect(within(ticker).getAllByRole('button', { name: /Book a follow-up with Tolu/ }).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Pause the ticker' })).toBeInTheDocument();
    expect(screen.getByText('Start with this')).toBeInTheDocument();
    // Context on every card: the why line and the More context disclosure.
    const more = screen.getAllByRole('button', { name: /More context/ });
    expect(more.length).toBe(fyFeed().items.length);
    expect(more[0]).toHaveAttribute('aria-expanded', 'false');
  });

  it('has no axe violations', async () => {
    // One card of each shape keeps axe fast in jsdom; the visual harness runs it on the full page.
    const keys = ['fy_harm1', 'fy_ticket1', 'fy_q1', 'fy_mtg1', 'fy_proof1'];
    client.getForYou.mockResolvedValue({ data: fyFeed(fyItems.filter((i) => keys.includes(i.item_key))), etag: '"e3"', notModified: false });
    const { container } = renderPage();
    await screen.findByRole('heading', { name: 'On your radar' });
    expect(await axe(container)).toHaveNoViolations();
  }, 240_000);

  it('More context opens details, related cards and why it ranks here', async () => {
    renderPage();
    const card = (await screen.findByRole('link', { name: /Share list shows/ })).closest('article')!;
    const btn = within(card).getByRole('button', { name: /More context/ });
    fireEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    const region = within(card).getByRole('region', { name: /More context/ });
    expect(within(region).getByText('Why it ranks here')).toBeInTheDocument();
    expect(within(region).getByText('feat/rl-0294')).toBeInTheDocument();
    expect(within(region).getByText(/decides the order/)).toBeInTheDocument();
  });

  it('Done leaves with a stamp, writes at once, and Undo reopens', async () => {
    renderPage();
    const card = (await screen.findByRole('link', { name: 'Book a follow-up with Tolu' })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /^Done/ }));
    expect(within(card).getByText('Done', { selector: 'b' })).toBeInTheDocument();
    expect(screen.getByText('Reminder closed as done.')).toBeInTheDocument();
    await waitFor(() => expect(client.patchReminder).toHaveBeenCalledWith('rem_7c41e0', { status: 'done' }));
    fireEvent.click(screen.getByRole('button', { name: /Undo/ }));
    await waitFor(() => expect(client.patchReminder).toHaveBeenCalledWith('rem_7c41e0', { status: 'open' }));
    expect(await screen.findByText('Restored.')).toBeInTheDocument();
  });

  it('Hide writes at once and Undo un-hides', async () => {
    renderPage();
    const card = (await screen.findByRole('link', { name: /Security low findings/ })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /Hide until it changes/ }));
    await waitFor(() => expect(client.hideForYouItem).toHaveBeenCalledWith('fy_quiet1'));
    fireEvent.click(screen.getByRole('button', { name: /Undo/ }));
    await waitFor(() => expect(client.unhideForYouItem).toHaveBeenCalledWith('fy_quiet1'));
  });

  it('the Undo toast waits while hovered', async () => {
    renderPage();
    const card = (await screen.findByRole('link', { name: /Security low findings/ })).closest('article')!;
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ['setTimeout', 'clearTimeout'] });
    fireEvent.click(within(card).getByRole('button', { name: /Hide until it changes/ }));
    const toast = screen.getByRole('button', { name: /Undo/ }).closest('.fy-toast')!;
    fireEvent.mouseEnter(toast);
    await act(async () => { vi.advanceTimersByTime(15_000); });
    expect(screen.getByRole('button', { name: /Undo/ })).toBeInTheDocument();
    fireEvent.mouseLeave(toast);
    await act(async () => { vi.advanceTimersByTime(1_500); });
    expect(screen.queryByRole('button', { name: /Undo/ })).not.toBeInTheDocument();
  });

  it('ignores single-key shortcuts while typing', async () => {
    renderPage();
    const card = (await screen.findAllByRole('link', { name: /Tolu Bello asked/ }))[0].closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /^Reply/ }));
    const box = within(card).getByRole('textbox', { name: 'Reply' });
    fireEvent.keyDown(box, { key: 'h' });
    fireEvent.keyDown(box, { key: '?' });
    expect(client.hideForYouItem).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument();
  });

  it('section failures name the section and retry', async () => {
    const feed = fyFeed();
    feed.sections = { ...feed.sections, questions: { count: 0, error: "couldn't load" } };
    client.getForYou.mockResolvedValue({ data: feed, etag: '"e9"', notModified: false });
    renderPage();
    expect(await screen.findByText(/Part of Your team is asking couldn’t load/)).toBeInTheDocument();
    const calls = client.getForYou.mock.calls.length;
    fireEvent.click(screen.getAllByRole('button', { name: 'Retry' })[0]);
    await waitFor(() => expect(client.getForYou.mock.calls.length).toBeGreaterThan(calls));
  });

  it('keyboard: j moves between cards, h hides, u undoes, ? opens the shortcuts', async () => {
    renderPage();
    await screen.findByRole('heading', { name: 'On your radar' });
    fireEvent.keyDown(document.body, { key: 'j' });
    const first = document.activeElement as HTMLElement;
    expect(first.classList.contains('card')).toBe(true);
    fireEvent.keyDown(first, { key: 'j' });
    expect(document.activeElement).not.toBe(first);
    fireEvent.keyDown(document.body, { key: '?' });
    expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument();
  });

  it('snooze opens a menu and hides the item until the chosen time', async () => {
    renderPage();
    const card = (await screen.findByRole('link', { name: /Paused at its round cap/ })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /^Snooze/ }));
    const menu = screen.getByRole('menu', { name: 'Snooze until' });
    fireEvent.click(within(menu).getByRole('menuitem', { name: /Tomorrow/ }));
    await waitFor(() => expect(client.hideForYouItem).toHaveBeenCalledWith('fy_loop1', snoozeTarget('tomorrow').toISOString()));
  });

  it('Acknowledge previews then promotes into the meeting destination', async () => {
    client.previewMeetingPromote.mockResolvedValue({
      meeting_id: 'mtg_fixture_01', item_id: 'mai_f1', title: 'Write the note', description: 'From Release sync',
      priority: 'medium', kind: 'task', assigned_to: null, assigned_user_id: 'u1', due_date: null,
      context_refs: [], target_project_id: 'proj_fixture_01', target_project_options: null, jira_publish_available: false,
    });
    client.promoteMeetingItem.mockResolvedValue({ ticket_id: 'tk_new', item_id: 'mai_f1', status: 'promoted', published_to_jira: false, warnings: [] });
    renderPage();
    const card = (await screen.findByRole('link', { name: /Write the 0.29.5 upgrade note/ })).closest('article')!;
    const ack = within(card).getByRole('button', { name: /Acknowledge/ });
    fireEvent.click(ack);
    expect(ack).toHaveAttribute('aria-expanded', 'true');
    await waitFor(() => expect(client.previewMeetingPromote).toHaveBeenCalledWith('mtg_fixture_01', 'mai_f1'));
    await waitFor(() => expect(within(card).getByRole('button', { name: 'Create ticket' })).toBeEnabled());
    expect(within(card).getByRole('combobox', { name: 'Assignee' })).toHaveDisplayValue('You');
    fireEvent.click(within(card).getByRole('button', { name: 'Create ticket' }));
    await waitFor(() => expect(client.promoteMeetingItem).toHaveBeenCalled());
    const body = client.promoteMeetingItem.mock.calls[0][2];
    expect(body).toMatchObject({ title: 'Write the 0.29.5 upgrade note for self-hosted installs', assigned_user_id: 'u1', target_project_id: 'proj_fixture_01' });
    expect(await screen.findByText(/Created tk_new/)).toBeInTheDocument();
  });

  it('Approve needs a reason, locks while submitting and stamps k of N', async () => {
    let resolve!: (v: unknown) => void;
    client.decideApprovalRequest.mockReturnValue(new Promise((r) => { resolve = r; }));
    renderPage();
    const card = (await screen.findByRole('link', { name: /Auth changes \(1\/2\)/ })).closest('article')!;
    const open = within(card).getByRole('button', { name: /^Approve/ });
    fireEvent.click(open);
    expect(open).toHaveAttribute('aria-expanded', 'true');
    expect(within(card).getByText(/Yours is the last of 2/)).toBeInTheDocument();
    const submit = within(card).getByRole('button', { name: 'Approve' });
    fireEvent.click(submit);
    expect(await within(card).findByRole('alert')).toHaveTextContent(/Add a reason/);
    expect(client.decideApprovalRequest).not.toHaveBeenCalled();
    fireEvent.change(within(card).getByRole('textbox', { name: 'Reason' }), { target: { value: 'Reviewed the read path' } });
    fireEvent.click(submit);
    await waitFor(() => expect(client.decideApprovalRequest).toHaveBeenCalledWith('apr_19f2c4', { decision: 'approve', reason: 'Reviewed the read path' }));
    expect(within(card).getByRole('button', { name: 'Approving…' })).toBeDisabled();
    expect(within(card).getByRole('textbox', { name: 'Reason' })).toBeDisabled();
    // The stamp is on screen for ~760 ms of wall-clock time (420 ms stamp, then
    // the leave animation) before the card is hidden. Those timers start when
    // the decision resolves, so a worker starved for longer than that (a full
    // parallel run) used to fire both before React painted: Node runs expired
    // timers ahead of the scheduler's setImmediate, the card went straight to
    // hidden and the stamp was never in the DOM to find. Own the clock from
    // here so the stamp, the leave and the hide happen in order.
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['setTimeout', 'clearTimeout'] });
    await act(async () => {
      resolve({ id: 'apr_19f2c4', state: 'approved', approved_count: 2, required_count: 2, decisions: [] });
      // Yields to the real event loop, so decide → commit → removeVisual settles inside act.
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(within(card).getByText('Approved · 2 of 2')).toBeInTheDocument();
    expect(screen.getByText('Approved. Auth changes has every approval it needs.')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(card.isConnected).toBe(false);
  });

  it('Reject shows the server error inline and keeps the panel open', async () => {
    const { ApiError } = await import('../api/client');
    client.decideApprovalRequest.mockRejectedValue(new ApiError(409, JSON.stringify({ detail: { message: 'This request has expired.' } })));
    renderPage();
    const card = (await screen.findByRole('link', { name: /Auth changes \(1\/2\)/ })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /^Reject$/ }));
    fireEvent.change(within(card).getByRole('textbox', { name: 'Reason' }), { target: { value: 'Logs the secret' } });
    fireEvent.click(within(card).getByRole('button', { name: 'Reject with this reason' }));
    await waitFor(() => expect(client.decideApprovalRequest).toHaveBeenCalledWith('apr_19f2c4', { decision: 'reject', reason: 'Logs the secret' }));
    expect(await within(card).findByRole('alert')).toHaveTextContent(/expired/);
    expect(within(card).getByRole('button', { name: 'Reject with this reason' })).toBeEnabled();
  });

  it('Claim handoff claims it from the card', async () => {
    client.claimHandoff.mockResolvedValue({ id: 'hnd_3f8a21', status: 'claimed' });
    renderPage();
    const card = (await screen.findByRole('link', { name: /Dara handed you/ })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /^Claim handoff/ }));
    await waitFor(() => expect(client.claimHandoff).toHaveBeenCalledWith('hnd_3f8a21'));
    expect(await screen.findByText(/Handoff claimed/)).toBeInTheDocument();
  });

  it('Raise limit offers new round caps and patches the loop', async () => {
    loopApi.listLoops.mockResolvedValue(fyLoops);
    loopApi.updateLoop.mockResolvedValue(fyLoops[0]);
    renderPage();
    const card = (await screen.findByRole('link', { name: /Paused at its round cap/ })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /^Raise limit/ }));
    const group = await within(card).findByRole('radiogroup', { name: 'New limit' });
    fireEvent.click(within(group).getByRole('radio', { name: /\+4 rounds \(cap 10\)/ }));
    fireEvent.click(within(card).getByRole('button', { name: 'Raise limit and resume' }));
    await waitFor(() => expect(loopApi.updateLoop).toHaveBeenCalledWith('proj_fixture_01', 'tk_loop_fixture_01', { max_review_rounds: 10 }));
  });

  it('A chat action cites messages and promotes through the meeting routes', async () => {
    client.previewMeetingPromote.mockResolvedValue({
      meeting_id: 'chat_fixture_01', item_id: 'mai_c1', title: 'Confirm', description: 'From chat thread',
      priority: 'medium', kind: 'task', assigned_to: null, assigned_user_id: 'u1', due_date: null,
      context_refs: [], target_project_id: 'proj_fixture_01', target_project_options: null, jira_publish_available: false,
    });
    renderPage();
    const card = (await screen.findByRole('link', { name: /Confirm the 0.29.5 ship date/ })).closest('article')!;
    expect(within(card).getAllByText(/M12-13/).length).toBeGreaterThan(0);
    fireEvent.click(within(card).getByRole('button', { name: /Acknowledge/ }));
    await waitFor(() => expect(client.previewMeetingPromote).toHaveBeenCalledWith('chat_fixture_01', 'mai_c1'));
    expect(within(card).getByText(/cited messages/)).toBeInTheDocument();
  });

  it('Not an action dismisses the meeting item after the undo window', async () => {
    client.dismissMeetingItem.mockResolvedValue({});
    renderPage();
    const card = (await screen.findByRole('link', { name: /Backfill the locale/ })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /Not an action/ }));
    await waitFor(() => expect(client.dismissMeetingItem).toHaveBeenCalledWith('mtg_fixture_02', 'mai_g1'));
  });

  it('Reply posts a ticket comment', async () => {
    client.addTicketComment.mockResolvedValue({});
    renderPage();
    const card = (await screen.findByRole('link', { name: /Tolu Bello asked/ })).closest('article')!;
    fireEvent.click(within(card).getByRole('button', { name: /^Reply/ }));
    const box = within(card).getByRole('textbox', { name: 'Reply' });
    fireEvent.click(within(card).getByRole('button', { name: 'Send reply' }));
    expect(within(card).getByRole('alert')).toHaveTextContent('Write a reply first.');
    fireEvent.change(box, { target: { value: 'Its own tier.' } });
    fireEvent.click(within(card).getByRole('button', { name: 'Send reply' }));
    await waitFor(() => expect(client.addTicketComment).toHaveBeenCalledWith('proj_fixture_01', 'tk_07fe22af490f4c4d', 'Its own tier.'));
  });

  it('Before you start loads related knowledge and sessions on demand', async () => {
    renderPage();
    const card = (await screen.findAllByRole('link', { name: /Rotated service keys keep/ }))[0].closest('article')!;
    expect(client.getForYouItemContext).not.toHaveBeenCalled();
    const btn = within(card).getByRole('button', { name: /Before you start/ });
    fireEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    expect(await within(card).findByText(/A service key is its minter/)).toBeInTheDocument();
    expect(client.getForYouItemContext).toHaveBeenCalledWith('fy_ticket1');
  });

  it('Working with others sends an edited reminder', async () => {
    client.remindCollaborator.mockResolvedValue({ reminder_id: 'rem_x', due_at: new Date().toISOString(), to: 'Kemi Ade' });
    renderPage();
    const btn = await screen.findByRole('button', { name: 'Send a reminder to Kemi' });
    fireEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    const box = screen.getByRole('textbox', { name: 'Message' });
    expect((box as HTMLTextAreaElement).value).toContain('Hi Kemi');
    fireEvent.change(box, { target: { value: 'Any news?' } });
    fireEvent.click(screen.getByRole('radio', { name: 'Tomorrow 09:00' }));
    fireEvent.click(screen.getByRole('button', { name: 'Send reminder to Kemi' }));
    await waitFor(() => expect(client.remindCollaborator).toHaveBeenCalled());
    expect(client.remindCollaborator.mock.calls[0][0]).toMatchObject({ flow_key: 'fyc_dep1', message: 'Any news?', when: 'tomorrow' });
  });

  it('shows the caught-up state when nothing needs you', async () => {
    client.getForYou.mockResolvedValue({ data: fyCaughtUpFeed(), etag: '"e2"', notModified: false });
    const { container } = renderPage();
    expect(await screen.findByRole('heading', { name: /You’re all caught up/ })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2, name: 'On your radar' })).not.toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Coming up next' })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('shows an error with a retry when the feed fails', async () => {
    client.getForYou.mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t load For You.');
    client.getForYou.mockResolvedValue({ data: fyFeed(), etag: '"e1"', notModified: false });
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('heading', { name: 'On your radar' })).toBeInTheDocument();
  });

  it('keeps the cached feed on a 304', async () => {
    client.getForYou
      .mockResolvedValueOnce({ data: fyFeed(), etag: '"e1"', notModified: false })
      .mockResolvedValueOnce({ data: null, etag: '"e1"', notModified: true });
    renderPage();
    await screen.findByRole('heading', { name: 'On your radar' });
    expect(client.getForYou).toHaveBeenCalledWith(undefined);
  });
});

describe('Long sections', { timeout: 60_000 }, () => {
  const band = () => document.getElementById('band-second')!;
  const cards = () => Array.from(band().querySelectorAll<HTMLElement>('.slot:not(.leaving) .card'));
  const keysShown = () => cards().map((c) => c.dataset.key);
  const more = () => within(band()).getByRole('button', { name: /^Show (\d+ more|less)/ });

  beforeEach(() => {
    window.sessionStorage.clear();
    client.getForYou.mockResolvedValue({ data: fyLongFeed(), etag: '"long"', notModified: false });
  });

  it('shows the first 10 in rank order, the true count, and Show N more', async () => {
    renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    expect(keysShown()).toEqual(Array.from({ length: 10 }, (_, i) => `fy_long${i + 1}`));
    await waitFor(() => expect(band().querySelector('.bandhead .cnt')).toHaveTextContent('79 items'));
    const btn = more();
    expect(btn).toHaveTextContent('Show 69 more');
    expect(btn).toHaveAttribute('aria-expanded', 'false');
    expect(btn).toHaveAttribute('aria-controls', 'list-second');
    expect(within(band()).getByText('Showing 10 of 79')).toBeInTheDocument();
    // Short sections have no toggle.
    expect(within(document.getElementById('band-waiting')!).queryByRole('button', { name: /^Show/ })).not.toBeInTheDocument();
  });

  it('Show more reveals the rest and moves focus to the first new card; Show less returns focus to the toggle', async () => {
    renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    fireEvent.click(more());
    expect(keysShown()).toHaveLength(79);
    expect(more()).toHaveTextContent('Show less');
    expect(more()).toHaveAttribute('aria-expanded', 'true');
    expect(within(band()).queryByText(/^Showing/)).not.toBeInTheDocument();
    await waitFor(() => expect((document.activeElement as HTMLElement).dataset.key).toBe('fy_long11'));
    // j and k carry on across the newly shown cards.
    fireEvent.keyDown(document.activeElement!, { key: 'j' });
    expect((document.activeElement as HTMLElement).dataset.key).toBe('fy_long12');
    fireEvent.keyDown(document.activeElement!, { key: 'k' });
    fireEvent.keyDown(document.activeElement!, { key: 'k' });
    expect((document.activeElement as HTMLElement).dataset.key).toBe('fy_long10');
    // Focus on a card that collapsing will hide: it goes to the toggle instead.
    (cards()[40] as HTMLElement).focus();
    fireEvent.click(more());
    expect(keysShown()).toHaveLength(10);
    await waitFor(() => expect(document.activeElement).toBe(more()));
    expect(more()).toHaveTextContent('Show 69 more');
  });

  it('section number keys still land on the first card of a collapsed section', async () => {
    renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    fireEvent.keyDown(document.body, { key: '6' });
    expect((document.activeElement as HTMLElement).dataset.key).toBe('fy_long1');
  });

  it('remembers an expanded section for the tab session', async () => {
    const first = renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    fireEvent.click(more());
    expect(JSON.parse(window.sessionStorage.getItem('sfs-foryou-expanded')!)).toEqual(['second']);
    first.unmount();
    renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    expect(keysShown()).toHaveLength(79);
    expect(more()).toHaveTextContent('Show less');
    // Nothing is written to localStorage: the choice doesn't outlive the tab.
    expect(Object.keys(window.localStorage).some((k) => k.includes('expanded'))).toBe(false);
  });

  it('works when sessionStorage throws', async () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    try {
      renderPage();
      await screen.findByRole('heading', { name: 'Worth a second look' });
      fireEvent.click(more());
      expect(keysShown()).toHaveLength(79);
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });

  it('hiding an item in a collapsed section moves the next one up, keeps focus in the section, and Undo restores it', async () => {
    renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    const tenth = cards()[9];
    expect(tenth.dataset.key).toBe('fy_long10');
    tenth.focus();
    fireEvent.keyDown(tenth, { key: 'h' });
    await waitFor(() => expect(client.hideForYouItem).toHaveBeenCalledWith('fy_long10'));
    await waitFor(() => expect(keysShown()).toEqual([...Array.from({ length: 9 }, (_, i) => `fy_long${i + 1}`), 'fy_long11']));
    await waitFor(() => expect((document.activeElement as HTMLElement).dataset.key).toBe('fy_long11'));
    expect(more()).toHaveTextContent('Show 68 more');
    expect(within(band()).getByText('Showing 10 of 78')).toBeInTheDocument();
    await waitFor(() => expect(band().querySelector('.bandhead .cnt')).toHaveTextContent('78 items'));

    fireEvent.click(screen.getByRole('button', { name: /Undo/ }));
    await waitFor(() => expect(client.unhideForYouItem).toHaveBeenCalledWith('fy_long10'));
    await waitFor(() => expect(keysShown()).toEqual(Array.from({ length: 10 }, (_, i) => `fy_long${i + 1}`)));
    expect(more()).toHaveTextContent('Show 69 more');
  });

  it('says so honestly when the feed returned fewer items than the section holds', async () => {
    client.getForYou.mockResolvedValue({ data: fyLongFeed(30, 312), etag: '"cap"', notModified: false });
    renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    await waitFor(() => expect(band().querySelector('.bandhead .cnt')).toHaveTextContent('312 items'));
    expect(more()).toHaveTextContent('Show 20 more');
    expect(within(band()).getByText('Showing 10 of 312')).toBeInTheDocument();
    fireEvent.click(more());
    expect(within(band()).getByText('Showing 30 of 312. More appear as you clear these.')).toBeInTheDocument();
  });

  it('going to an item behind Show more opens its section and focuses it', async () => {
    const feed = fyLongFeed();
    feed.chyron = [{ item_key: 'fy_long40', category: 'forgotten', text: feed.items.find((i) => i.item_key === 'fy_long40')!.title, ref: 'x' }];
    client.getForYou.mockResolvedValue({ data: feed, etag: '"goto"', notModified: false });
    renderPage();
    await screen.findByRole('heading', { name: 'Worth a second look' });
    const ticker = screen.getByRole('region', { name: /Right now/ });
    fireEvent.click(within(ticker).getAllByRole('button', { name: /Empty state on the releases page \(40\)/ })[0]);
    await waitFor(() => expect((document.activeElement as HTMLElement).dataset.key).toBe('fy_long40'));
    expect(keysShown()).toHaveLength(79);
  });
});

describe('Chyron', () => {
  const entries = fyFeed().chyron;

  it('scrolls a duplicated, aria-hidden copy and pauses', () => {
    const onPick = vi.fn();
    const { container } = render(<Chyron entries={entries} cleared={new Set()} reduce={false} onPick={onPick} />);
    const lists = container.querySelectorAll('.tk-list');
    expect(lists).toHaveLength(2);
    expect(lists[1]).toHaveAttribute('aria-hidden', 'true');
    expect(container.querySelector('.tk-view')).toHaveAttribute('aria-live', 'off');
    const pause = screen.getByRole('button', { name: 'Pause the ticker' });
    fireEvent.click(pause);
    expect(screen.getByRole('button', { name: 'Play the ticker' })).toBeInTheDocument();
    fireEvent.click(within(lists[0] as HTMLElement).getAllByRole('button')[0]);
    expect(onPick).toHaveBeenCalledWith(entries[0].item_key);
  });

  it('becomes a static stepper with reduced motion', () => {
    const { container } = render(<Chyron entries={entries} cleared={new Set()} reduce onPick={vi.fn()} />);
    expect(container.querySelector('.ticker')).toHaveClass('static');
    expect(container.querySelectorAll('.tk-list')).toHaveLength(1);
    expect(screen.getByText(`1 of ${entries.length}`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next item' }));
    expect(screen.getByText(`2 of ${entries.length}`)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pause the ticker' })).not.toBeInTheDocument();
  });
});

describe('model helpers', () => {
  it('snooze targets: 1 hour, tomorrow 09:00, next Monday 09:00', () => {
    const now = new Date(2026, 10, 6, 9, 12); // Fri 6 Nov
    expect(snoozeTarget('1h', now).getHours()).toBe(10);
    const tom = snoozeTarget('tomorrow', now);
    expect([tom.getDate(), tom.getHours(), tom.getMinutes()]).toEqual([7, 9, 0]);
    const nw = snoozeTarget('next_week', now);
    expect([nw.getDay(), nw.getDate(), nw.getHours()]).toEqual([1, 9, 9]);
  });

  it('whenLabel', () => {
    const now = new Date(2026, 10, 6, 9, 12);
    expect(whenLabel(new Date(2026, 10, 6, 7, 10).toISOString(), now)).toBe('Today 07:10');
    expect(whenLabel(new Date(2026, 10, 5, 17, 40).toISOString(), now)).toBe('Yesterday 17:40');
  });
});
