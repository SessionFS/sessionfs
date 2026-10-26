import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type TicketPreview as TicketPreviewData } from '../api/client';
import { Markdown } from '../components/ui';
import { axe } from '../test/axe';
import { remarkRefs } from './format';
import { HelperRefsProvider, renderRefLink } from './refs';

const { getTicketById, getProject } = vi.hoisted(() => ({
  getTicketById: vi.fn(),
  getProject: vi.fn(),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { apiKey: 'sk_test', baseUrl: 'http://test.api', client: { getTicketById, getProject } } }),
}));

const TK = 'tk_008676e806e94bc5';
const PJ = 'proj_a40ce92c16e9415d';

const ticket: TicketPreviewData = {
  id: TK,
  project_id: PJ,
  project_name: 'Acme Widgets',
  title: 'Retry multipart uploads after a 503',
  description: 'The client gives up on the **first** 503.\n\nIt should back off and retry.',
  priority: 'high',
  assigned_to: 'atlas',
  created_by_user_id: 'u1',
  created_by_session_id: null,
  created_by_persona: null,
  status: 'in_progress',
  kind: 'task',
  parent_ticket_id: null,
  child_ticket_ids: [],
  context_refs: [],
  file_refs: [],
  related_sessions: [],
  acceptance_criteria: ['retries 3 times', 'jittered backoff'],
  resolver_session_id: null,
  resolver_user_id: null,
  completion_notes: null,
  changed_files: [],
  knowledge_entry_ids: [],
  depends_on: [],
  created_at: '2026-10-20T10:00:00Z',
  updated_at: '2026-10-24T10:00:00Z',
  resolved_at: null,
  comment_count: 5,
  recent_comments: [
    {
      id: 'tc_3',
      ticket_id: TK,
      author_user_id: 'u1',
      author_persona: 'codex-reviewer',
      content: 'VERIFIED-CLEAN',
      session_id: null,
      created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    },
    {
      id: 'tc_2',
      ticket_id: TK,
      author_user_id: 'u1',
      author_persona: null,
      content: 'Pushed the fix.',
      session_id: null,
      created_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    },
  ],
};

const PLUGINS = [remarkRefs()];

function Answer({ text }: { text: string }) {
  return (
    <Markdown renderLink={renderRefLink} remarkPlugins={PLUGINS}>
      {text}
    </Markdown>
  );
}

function renderAnswer(text = `Ticket ${TK} in project ${PJ} tracks it.`, onNavigate = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div className="relative h-[600px]">
          <HelperRefsProvider onNavigate={onNavigate}>
            <Answer text={text} />
          </HelperRefsProvider>
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, onNavigate };
}

beforeEach(() => {
  getTicketById.mockReset();
  getProject.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('ticket references in helper answers', () => {
  it('renders ids as a preview button and an in-app project link', () => {
    renderAnswer();
    expect(screen.getByRole('button', { name: `Preview ticket ${TK}` })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('link', { name: PJ })).toHaveAttribute('href', `/projects/${PJ}`);
  });

  it('shows a loading skeleton, then the ticket', async () => {
    let resolve!: (t: TicketPreviewData) => void;
    getTicketById.mockReturnValue(new Promise((r) => (resolve = r)));
    const user = userEvent.setup();
    renderAnswer();

    await user.click(screen.getByRole('button', { name: `Preview ticket ${TK}` }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-labelledby');
    expect(dialog.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(getTicketById).toHaveBeenCalledWith(TK);

    await act(async () => resolve(ticket));
    expect(await screen.findByRole('heading', { name: ticket.title })).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: ticket.title })).toBeInTheDocument();
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByText('atlas')).toBeInTheDocument();
    expect(screen.getByText('Acme Widgets')).toBeInTheDocument();
    expect(screen.getByText('2 acceptance criteria')).toBeInTheDocument();
    expect(screen.getByText('VERIFIED-CLEAN')).toBeInTheDocument();
    expect(screen.getByText('5 min ago')).toBeInTheDocument();
    expect(screen.getByText(/5 total/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open ticket' })).toHaveAttribute(
      'href',
      `/projects/${PJ}?tab=work&sub=tickets&ticket=${TK}`,
    );
    expect(screen.getByRole('button', { name: `Preview ticket ${TK}` })).toHaveAttribute('aria-expanded', 'true');
  });

  it('says the ticket is not available on a 404, without a retry', async () => {
    getTicketById.mockRejectedValue(new ApiError(404, '{"detail":"Ticket not found"}'));
    const user = userEvent.setup();
    renderAnswer();
    await user.click(screen.getByRole('button', { name: `Preview ticket ${TK}` }));
    expect(await screen.findByRole('heading', { name: 'Ticket not available' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('offers a retry on other errors', async () => {
    // The hook retries a 5xx once before showing the error.
    getTicketById
      .mockRejectedValueOnce(new ApiError(500, '{"detail":"boom"}'))
      .mockRejectedValueOnce(new ApiError(500, '{"detail":"boom"}'))
      .mockResolvedValueOnce(ticket);
    const user = userEvent.setup();
    renderAnswer();
    await user.click(screen.getByRole('button', { name: `Preview ticket ${TK}` }));
    expect(
      await screen.findByRole('heading', { name: 'Couldn’t load this ticket' }, { timeout: 4000 }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('heading', { name: ticket.title })).toBeInTheDocument();
  });

  it('closes on Escape without letting the key reach the drawer, and returns focus', async () => {
    getTicketById.mockResolvedValue(ticket);
    const drawerEscape = vi.fn();
    const onDrawerKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) drawerEscape();
    };
    document.addEventListener('keydown', onDrawerKey);
    const user = userEvent.setup();
    renderAnswer();
    const trigger = screen.getByRole('button', { name: `Preview ticket ${TK}` });
    await user.click(trigger);
    await screen.findByRole('heading', { name: ticket.title });
    expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true);

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(drawerEscape).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();
    document.removeEventListener('keydown', onDrawerKey);
  });

  it('keeps Tab inside the preview and closes from the close button', async () => {
    getTicketById.mockResolvedValue(ticket);
    const user = userEvent.setup();
    renderAnswer();
    const trigger = screen.getByRole('button', { name: `Preview ticket ${TK}` });
    await user.click(trigger);
    await screen.findByRole('heading', { name: ticket.title });
    const dialog = screen.getByRole('dialog');
    for (let i = 0; i < 4; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.click(screen.getByRole('button', { name: 'Close ticket preview' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it('closes the preview and the drawer when opening the full ticket', async () => {
    getTicketById.mockResolvedValue(ticket);
    const user = userEvent.setup();
    const { onNavigate } = renderAnswer();
    await user.click(screen.getByRole('button', { name: `Preview ticket ${TK}` }));
    await user.click(await screen.findByRole('link', { name: 'Open ticket' }));
    expect(onNavigate).toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('has no axe violations when open', async () => {
    getTicketById.mockResolvedValue(ticket);
    const user = userEvent.setup();
    const { container } = renderAnswer();
    await user.click(screen.getByRole('button', { name: `Preview ticket ${TK}` }));
    await screen.findByRole('heading', { name: ticket.title });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('has no axe violations in the not-available state', async () => {
    getTicketById.mockRejectedValue(new ApiError(404, '{"detail":"Ticket not found"}'));
    const user = userEvent.setup();
    const { container } = renderAnswer();
    await user.click(screen.getByRole('button', { name: `Preview ticket ${TK}` }));
    await screen.findByRole('heading', { name: 'Ticket not available' });
    expect(await axe(container)).toHaveNoViolations();
  });
});
