/**
 * Tests for the ticket notification inbox. Hooks are mocked so the suite
 * focuses on the UI: kind badges, ticket title links, relative time, and the
 * mark-read / mark-all-read wiring.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import TicketInbox from './TicketInbox';

const { hooks, toastHook, toastApi } = vi.hoisted(() => {
  const toastApi = { addToast: vi.fn() };
  return {
    hooks: {
      useTicketNotifications: vi.fn(),
      useMarkTicketNotificationsRead: vi.fn(),
    },
    toastHook: { useToast: vi.fn() },
    toastApi,
  };
});

vi.mock('../hooks/useTicketNotifications', () => hooks);
vi.mock('../hooks/useToast', () => toastHook);

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    isPending: false,
    ...extra,
  };
}

function notification(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    ticket_id: 'tk_1',
    project_id: 'proj_1',
    ticket_title: 'Add session export',
    kind: 'your_turn',
    summary: 'The ticket is ready for your review.',
    actor_type: 'user',
    actor_user_id: 'u2',
    service_key_name: null,
    event_count: 1,
    created_at: '2026-05-14T01:00:00Z',
    updated_at: '2026-05-14T01:00:00Z',
    read_at: null,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <TicketInbox />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  toastHook.useToast.mockReset();
  toastApi.addToast.mockReset();

  hooks.useTicketNotifications.mockReturnValue({
    data: { items: [notification()], next_before: null },
    isLoading: false,
    error: null,
  });
  hooks.useMarkTicketNotificationsRead.mockReturnValue(makeMutation());
  toastHook.useToast.mockReturnValue(toastApi);
});

describe('TicketInbox', () => {
  it('renders a kind badge, title link, summary and relative time', () => {
    renderPage();
    expect(screen.getByText('Your turn')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Add session export' });
    expect(link).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=tickets&ticket=tk_1',
    );
    expect(screen.getByText('The ticket is ready for your review.')).toBeInTheDocument();
  });

  it('defaults to unread-only and toggles to show all', async () => {
    const user = userEvent.setup();
    renderPage();
    expect(hooks.useTicketNotifications).toHaveBeenCalledWith({
      unread: true,
      limit: 100,
    });
    await user.click(screen.getByRole('button', { name: 'Show all' }));
    expect(hooks.useTicketNotifications).toHaveBeenCalledWith({
      unread: false,
      limit: 100,
    });
  });

  it('marks a single notification read by id', async () => {
    const markRead = makeMutation();
    hooks.useMarkTicketNotificationsRead.mockReturnValue(markRead);
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('button', { name: /mark notification 1 read/i }));
    expect(markRead.mutate).toHaveBeenCalledWith(
      { ids: [1] },
      expect.any(Object),
    );
  });

  it('marks all read with all=true', async () => {
    const markRead = makeMutation();
    markRead.mutate.mockImplementation((_body: unknown, opts: { onSuccess?: () => void }) =>
      opts?.onSuccess?.(),
    );
    hooks.useMarkTicketNotificationsRead.mockReturnValue(markRead);
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('button', { name: 'Mark all read' }));
    expect(markRead.mutate).toHaveBeenCalledWith({ all: true }, expect.any(Object));
    expect(toastApi.addToast).toHaveBeenCalledWith(
      'success',
      'All notifications marked read',
    );
  });

  it('shows an empty state when there are no unread notifications', () => {
    hooks.useTicketNotifications.mockReturnValue({
      data: { items: [], next_before: null },
      isLoading: false,
      error: null,
    });
    renderPage();
    expect(screen.getByText('No unread notifications.')).toBeInTheDocument();
  });

  it('hides the mark-read button on already-read rows', () => {
    hooks.useTicketNotifications.mockReturnValue({
      data: {
        items: [notification({ read_at: '2026-05-14T02:00:00Z' })],
        next_before: null,
      },
      isLoading: false,
      error: null,
    });
    renderPage();
    expect(
      screen.queryByRole('button', { name: /mark notification 1 read/i }),
    ).toBeNull();
  });
});
