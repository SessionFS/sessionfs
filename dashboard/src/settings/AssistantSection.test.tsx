/**
 * Settings → Assistant: the plan-gated note on a 403, the personal form
 * (no ceilings or budget, 2000-character instructions) and the org-key note.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type UserHelperSettings } from '../api/client';
import { axe } from '../test/axe';
import AssistantSection from './AssistantSection';

const { hooks, statusHook } = vi.hoisted(() => ({
  hooks: { useUserHelperSettings: vi.fn(), useSaveUserHelperSettings: vi.fn() },
  statusHook: { useHelperStatus: vi.fn() },
}));

vi.mock('../helper/useHelperSettings', () => hooks);
vi.mock('../helper/useHelper', () => statusHook);
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));

const DATA: UserHelperSettings = {
  settings: {
    max_output_tokens: 4096,
    temperature: 0,
    top_p: null,
    max_tool_steps: 8,
    reasoning_effort: null,
    answer_style: 'concise',
  },
  instructions: null,
  effective_settings: null,
  usage: { input_tokens: 1200, output_tokens: 300, total_tokens: 1500 },
};

const UPGRADE = new ApiError(
  403,
  JSON.stringify({ error: { code: 'upgrade_required', message: 'Requires enterprise.', details: { feature: 'session_helper' } } }),
);

let mutate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mutate = vi.fn();
  hooks.useUserHelperSettings.mockReturnValue({ data: DATA, isLoading: false, isError: false });
  hooks.useSaveUserHelperSettings.mockReturnValue({ mutate, isPending: false });
  statusHook.useHelperStatus.mockReturnValue({ data: { enabled: true, key_source: 'user' } });
});

function renderSection() {
  return render(
    <MemoryRouter>
      <AssistantSection />
    </MemoryRouter>,
  );
}

describe('AssistantSection', () => {
  it('shows "Available on Enterprise" instead of the form on a 403', async () => {
    hooks.useUserHelperSettings.mockReturnValue({ data: undefined, isLoading: false, isError: true, error: UPGRADE });
    const { container } = renderSection();
    expect(screen.getByText('Available on Enterprise')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View plans' })).toHaveAttribute('href', '/settings/billing');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('shows a load error (not the plan note) for other failures', () => {
    hooks.useUserHelperSettings.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(500, 'boom'),
      refetch: vi.fn(),
    });
    renderSection();
    expect(screen.getByText("Couldn't load the assistant settings")).toBeInTheDocument();
    expect(screen.queryByText('Available on Enterprise')).toBeNull();
  });

  it('has model settings and personal instructions but no ceilings or budget', async () => {
    const user = userEvent.setup();
    const { container } = renderSection();
    expect(screen.getByRole('textbox', { name: 'Max output tokens' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /ceiling/i })).toBeNull();
    expect(screen.queryByRole('textbox', { name: /budget/i })).toBeNull();
    const box = screen.getByRole('textbox', { name: 'Instructions for the assistant' });
    expect(box).toHaveAccessibleDescription(/0 \/ 2,000 characters/);
    expect(screen.getByText('1.5k')).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    await user.type(box, 'Call me Alex.');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate.mock.calls[0][0]).toEqual({ instructions: 'Call me Alex.' });
  });

  it('says the org settings apply while turns use the org key', () => {
    statusHook.useHelperStatus.mockReturnValue({ data: { enabled: true, key_source: 'org' } });
    renderSection();
    expect(screen.getByText("You're using your org's key")).toBeInTheDocument();
  });
});
