/**
 * Org Policies → Assistant: admin edit (only changed fields are sent, reset
 * sends null, 422s land on their field, live character counter) and the
 * read-only member view. Data hooks are mocked; the form logic is real.
 */

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type OrgHelperSettings } from '../api/client';
import { axe } from '../test/axe';
import OrgAssistantSection from './OrgAssistantSection';

const { hooks, membersHook, toastApi } = vi.hoisted(() => ({
  hooks: { useOrgHelperSettings: vi.fn(), useSaveOrgHelperSettings: vi.fn() },
  membersHook: { useOrgMembers: vi.fn() },
  toastApi: { addToast: vi.fn() },
}));

vi.mock('../helper/useHelperSettings', () => hooks);
vi.mock('./useOrgMembers', () => membersHook);
vi.mock('../hooks/useToast', () => ({ useToast: () => toastApi }));

const DEFAULTS = {
  max_output_tokens: 4096,
  temperature: 0,
  top_p: null,
  max_tool_steps: 8,
  reasoning_effort: null,
  answer_style: 'concise' as const,
};

const ADMIN: OrgHelperSettings = {
  role: 'admin',
  plan_includes_helper: true,
  effective_settings: { ...DEFAULTS, max_output_tokens: 3000 },
  has_instructions: true,
  settings: { ...DEFAULTS, max_output_tokens: 8000, ceiling_max_output_tokens: 3000, ceiling_max_tool_steps: null },
  instructions: 'Cite the newest session first.',
  monthly_token_budget: 2_000_000,
  usage: {
    input_tokens: 1_300_000,
    output_tokens: 200_000,
    total_tokens: 1_500_000,
    per_member: [
      { user_id: 'u1', input_tokens: 1_000_000, output_tokens: 150_000, total_tokens: 1_150_000 },
      { user_id: 'u_gone', input_tokens: 300_000, output_tokens: 50_000, total_tokens: 350_000 },
    ],
  },
};

const MEMBER: OrgHelperSettings = {
  role: 'member',
  plan_includes_helper: true,
  effective_settings: { ...DEFAULTS, max_output_tokens: 3000, temperature: null },
  has_instructions: false,
  usage: { input_tokens: 4000, output_tokens: 1200, total_tokens: 5200 },
};

let mutate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mutate = vi.fn();
  toastApi.addToast.mockReset();
  membersHook.useOrgMembers.mockClear();
  hooks.useOrgHelperSettings.mockReturnValue({ data: ADMIN, isLoading: false, isError: false });
  hooks.useSaveOrgHelperSettings.mockReturnValue({ mutate, isPending: false });
  membersHook.useOrgMembers.mockReturnValue({
    data: { members: [{ user_id: 'u1', email: 'ada@example.test', display_name: 'Ada', role: 'admin', joined_at: null }] },
    isLoading: false,
  });
});

const field = (name: string) => screen.getByRole('textbox', { name });

describe('OrgAssistantSection — admin', () => {
  it('shows the stored values, the budget meter and per-member usage with names', () => {
    render(<OrgAssistantSection orgId="org_1" />);
    expect(field('Max output tokens')).toHaveValue('8000');
    expect(field('Output token ceiling')).toHaveValue('3000');
    expect(field('Monthly token budget')).toHaveValue('2000000');
    expect(screen.getByRole('progressbar', { name: /used this month/i })).toHaveAttribute('aria-valuenow', '75');
    const table = screen.getByRole('table');
    expect(within(table).getByRole('rowheader', { name: /Ada/ })).toHaveTextContent('ada@example.test');
    expect(within(table).getByRole('rowheader', { name: /Former member/ })).toHaveTextContent('u_gone');
    // Ties the hint to the input.
    expect(field('Max output tokens')).toHaveAccessibleDescription(/256–32,000 tokens per answer/);
  });

  it('saves only the fields that changed', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    const save = screen.getByRole('button', { name: 'Save changes' });
    expect(save).toBeDisabled();

    await user.clear(field('Max tool steps'));
    await user.type(field('Max tool steps'), '5');
    await user.clear(field('Monthly token budget'));
    await user.click(save);

    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toEqual({ settings: { max_tool_steps: 5 }, monthly_token_budget: null });
  });

  it('"Reset to default" sends an explicit null for that field', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    await user.click(screen.getByRole('button', { name: 'Reset max output tokens to default' }));
    expect(field('Max output tokens')).toHaveValue('4096');
    expect(screen.getByText(/Resets to the default \(4,096\) when you save/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate.mock.calls[0][0]).toEqual({ settings: { max_output_tokens: null } });
  });

  it('shows a 422 on the field it belongs to', async () => {
    const user = userEvent.setup();
    mutate.mockImplementation((_body, opts) =>
      opts.onError(
        new ApiError(
          422,
          JSON.stringify({
            error: {
              code: '422',
              message: 'Validation error',
              details: {
                errors: [
                  { loc: ['body', 'settings', 'ceiling_max_tool_steps'], msg: 'Input should be less than or equal to 20' },
                ],
              },
            },
          }),
        ),
      ),
    );
    render(<OrgAssistantSection orgId="org_1" />);
    await user.type(field('Tool step ceiling'), '12');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    const input = field('Tool step ceiling');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAccessibleDescription(/Must be less than or equal to 20/);
    expect(toastApi.addToast).not.toHaveBeenCalled();
  });

  it('maps instruction errors to the instructions field', async () => {
    const user = userEvent.setup();
    mutate.mockImplementation((_body, opts) =>
      opts.onError(
        new ApiError(
          422,
          JSON.stringify({ error: { code: 'instructions_invalid', message: 'Custom instructions cannot contain tags.' } }),
        ),
      ),
    );
    render(<OrgAssistantSection orgId="org_1" />);
    const box = screen.getByRole('textbox', { name: 'Instructions for the assistant' });
    await user.type(box, ' More.');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(box).toHaveAccessibleDescription(/cannot contain tags/);
  });

  it('counts characters live against the 4000 limit and blocks an over-long save', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    const box = screen.getByRole('textbox', { name: 'Instructions for the assistant' });
    expect(box).toHaveAccessibleDescription(/30 \/ 4,000 characters/);
    await user.clear(box);
    await user.click(box);
    await user.paste('x'.repeat(4005));
    expect(box).toHaveAccessibleDescription(/4,005 \/ 4,000 characters/);
    expect(screen.getByRole('status')).toHaveTextContent('Over the 4,000-character limit.');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate).not.toHaveBeenCalled();
    expect(box).toHaveAccessibleDescription(/Use at most 4,000 characters/);
  });

  it('never turns unparseable budget or ceiling input into null', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    await user.clear(field('Monthly token budget'));
    await user.type(field('Monthly token budget'), '2e6');
    await user.type(field('Tool step ceiling'), '+3');
    await user.clear(field('Top P (optional)'));
    await user.type(field('Top P (optional)'), '0..5');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate).not.toHaveBeenCalled();
    expect(field('Monthly token budget')).toHaveAttribute('aria-invalid', 'true');
    expect(field('Tool step ceiling')).toHaveAttribute('aria-invalid', 'true');
    expect(field('Top P (optional)')).toHaveAttribute('aria-invalid', 'true');
  });

  it('accepts a decimal comma in Top P', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    await user.clear(field('Top P (optional)'));
    await user.type(field('Top P (optional)'), '0,5');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate.mock.calls[0][0]).toEqual({ settings: { top_p: 0.5 } });
  });

  it('accepts digit grouping in the budget', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    await user.clear(field('Monthly token budget'));
    await user.type(field('Monthly token budget'), '6,000,000');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate.mock.calls[0][0]).toEqual({ monthly_token_budget: 6_000_000 });
  });

  it('counts instruction characters as code points, like the server', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    const box = screen.getByRole('textbox', { name: 'Instructions for the assistant' });
    await user.clear(box);
    await user.click(box);
    // 2000 emoji = 4000 UTF-16 units but 2000 characters on the server.
    await user.paste('😀'.repeat(2000));
    expect(box).toHaveAccessibleDescription(/2,000 \/ 4,000 characters/);
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it('disables the temperature slider while the number is invalid', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    const slider = screen.getByRole('slider', { name: 'Temperature slider' });
    expect(slider).toBeEnabled();
    await user.clear(field('Temperature'));
    await user.type(field('Temperature'), '0.15');
    expect(slider).toHaveValue('0.15');
    await user.clear(field('Temperature'));
    await user.type(field('Temperature'), '3');
    expect(slider).toBeDisabled();
    expect(slider).toHaveAttribute('aria-valuetext', 'Not set');
  });

  it('sends null for temperature when "Provider default" is ticked', async () => {
    const user = userEvent.setup();
    render(<OrgAssistantSection orgId="org_1" />);
    await user.click(screen.getByRole('checkbox', { name: /Provider default/ }));
    expect(field('Temperature')).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(mutate.mock.calls[0][0]).toEqual({ settings: { temperature: null } });
  });

  it('has no axe violations', async () => {
    const { container } = render(<OrgAssistantSection orgId="org_1" />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('OrgAssistantSection — member', () => {
  beforeEach(() => {
    hooks.useOrgHelperSettings.mockReturnValue({ data: MEMBER, isLoading: false, isError: false });
  });

  it('shows effective values and own usage, read-only', () => {
    render(<OrgAssistantSection orgId="org_1" />);
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
    expect(screen.queryByRole('table')).toBeNull();
    const list = screen.getByText('Max output tokens').closest('dl')!;
    expect(list).toHaveTextContent('Max output tokens3,000');
    expect(list).toHaveTextContent('TemperatureProvider default');
    expect(screen.getByText('Your usage this month')).toBeInTheDocument();
    expect(screen.getByText('5.2k')).toBeInTheDocument();
    expect(membersHook.useOrgMembers).not.toHaveBeenCalled();
  });

  it('has no axe violations', async () => {
    const { container } = render(<OrgAssistantSection orgId="org_1" />);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('shows how much of the org budget is used, as a percentage only', () => {
    hooks.useOrgHelperSettings.mockReturnValue({
      data: { ...MEMBER, budget_reached: false, budget_used_pct: 76 },
      isLoading: false,
      isError: false,
    });
    render(<OrgAssistantSection orgId="org_1" />);
    expect(screen.getByRole('progressbar', { name: /org budget used/i })).toHaveAttribute('aria-valuenow', '76');
    expect(screen.queryByText('Monthly budget reached')).toBeNull();
  });

  it('tells members when the monthly budget is reached', async () => {
    hooks.useOrgHelperSettings.mockReturnValue({
      data: { ...MEMBER, budget_reached: true, budget_used_pct: 100 },
      isLoading: false,
      isError: false,
    });
    const { container } = render(<OrgAssistantSection orgId="org_1" />);
    expect(screen.getByText('Monthly budget reached')).toBeInTheDocument();
    expect(screen.getByText(/paused until the 1st/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('OrgAssistantSection — plan without the helper', () => {
  it('shows the Enterprise note and disables editing for admins', () => {
    hooks.useOrgHelperSettings.mockReturnValue({
      data: { ...ADMIN, plan_includes_helper: false },
      isLoading: false,
      isError: false,
    });
    render(<OrgAssistantSection orgId="org_1" />);
    expect(screen.getByText('Available on Enterprise')).toBeInTheDocument();
    expect(field('Max output tokens')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  });
});
