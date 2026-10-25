/**
 * Tests for the Preferences section's notification email preference (added
 * alongside ticket watchers). The notification hooks and toast are mocked so
 * the suite focuses on the select wiring.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PreferencesSection from './PreferencesSection';

const { hooks, toastHook } = vi.hoisted(() => {
  return {
    hooks: {
      useNotificationSettings: vi.fn(),
      useUpdateNotificationSettings: vi.fn(),
    },
    toastHook: { useToast: vi.fn() },
  };
});

vi.mock('../hooks/useTicketNotifications', () => hooks);
vi.mock('../hooks/useToast', () => toastHook);

function makeMutation() {
  return { mutate: vi.fn(), isPending: false };
}

beforeEach(() => {
  hooks.useNotificationSettings.mockReset();
  hooks.useUpdateNotificationSettings.mockReset();
  toastHook.useToast.mockReset();

  hooks.useNotificationSettings.mockReturnValue({ data: { ticket_email: 'your_turn' } });
  hooks.useUpdateNotificationSettings.mockReturnValue(makeMutation());
  toastHook.useToast.mockReturnValue({ addToast: vi.fn() });
});

describe('PreferencesSection notifications', () => {
  it('renders the ticket email preference with the stored value', () => {
    render(<PreferencesSection />);
    expect(screen.getByText('Notifications')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ticket email' })).toHaveTextContent(
      'Your turn only',
    );
  });

  it('saves a new email preference through the update mutation', async () => {
    const update = makeMutation();
    hooks.useUpdateNotificationSettings.mockReturnValue(update);
    const user = userEvent.setup();
    render(<PreferencesSection />);

    await user.click(screen.getByRole('button', { name: 'Ticket email' }));
    await user.click(screen.getByRole('option', { name: 'Off' }));

    expect(update.mutate).toHaveBeenCalledWith(
      { ticket_email: 'off' },
      expect.any(Object),
    );
  });
});
