/**
 * "Test connection" and the save-time Base URL check on the personal judge
 * key form (tk_e6880f06870541bf). Data hooks are mocked; the connection
 * test result rendering, field errors and placeholders are real.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { axe } from '../test/axe';
import AuditJudgeSettings from './AuditJudgeSettings';

const { judgeHooks, providersHook, toastApi, testConnection } = vi.hoisted(() => ({
  judgeHooks: {
    useJudgeSettings: vi.fn(),
    useSaveJudgeSettings: vi.fn(),
    useClearJudgeSettings: vi.fn(),
    useJudgeConnectionTester: vi.fn(),
  },
  providersHook: { useLlmProviders: vi.fn() },
  toastApi: { addToast: vi.fn() },
  testConnection: vi.fn(),
}));

vi.mock('../hooks/useJudgeSettings', () => judgeHooks);
vi.mock('../hooks/useLlmProviders', () => providersHook);
vi.mock('../hooks/useToast', () => ({ useToast: () => toastApi }));
// A stable auth object, as in the app: the form's discovery effect depends on it.
const { authValue } = vi.hoisted(() => ({
  authValue: { auth: { apiKey: 'sk_test', baseUrl: 'http://test.api', client: { discoverModels: async () => ({ models: [] }) } } },
}));
vi.mock('../auth/AuthContext', () => ({ useAuth: () => authValue }));

function makeMutation(extra: Record<string, unknown> = {}) {
  return { mutate: vi.fn(), isPending: false, isError: false, error: null, ...extra };
}

const PROVIDERS = [
  {
    id: 'deepseek',
    label: 'DeepSeek',
    api_style: 'openai',
    suggested_models: ['deepseek-flash', 'deepseek-v4-pro'],
    discovery: true,
    default_base_url: 'https://api.deepseek.com',
  },
  {
    id: 'anthropic',
    label: 'Anthropic',
    api_style: 'anthropic',
    suggested_models: ['claude-sonnet-5-5'],
    discovery: false,
    default_base_url: null,
  },
];

function saved(overrides: Record<string, unknown> = {}) {
  return {
    data: { provider: 'deepseek', model: 'deepseek-v4-pro', key_set: true, base_url: null, ...overrides },
    isLoading: false,
  };
}

beforeEach(() => {
  for (const h of Object.values(judgeHooks)) h.mockReset();
  testConnection.mockReset();
  toastApi.addToast.mockReset();
  judgeHooks.useJudgeSettings.mockReturnValue(saved());
  judgeHooks.useSaveJudgeSettings.mockReturnValue(makeMutation());
  judgeHooks.useClearJudgeSettings.mockReturnValue(makeMutation());
  judgeHooks.useJudgeConnectionTester.mockReturnValue(testConnection);
  providersHook.useLlmProviders.mockReturnValue({ data: { providers: PROVIDERS }, isLoading: false });
});

describe('AuditJudgeSettings — Test connection', () => {
  it("uses the provider's API address as the Base URL placeholder", () => {
    render(<AuditJudgeSettings />);
    const baseUrl = screen.getByLabelText(/^Base URL \(optional\)$/);
    expect(baseUrl).toHaveAttribute('placeholder', 'https://api.deepseek.com');
    expect(screen.getByText(/Leave empty to use DeepSeek's API/)).toBeInTheDocument();
  });

  it('falls back to a generic placeholder for providers used without a Base URL', () => {
    judgeHooks.useJudgeSettings.mockReturnValue(saved({ provider: 'anthropic', model: 'claude-sonnet-5-5' }));
    render(<AuditJudgeSettings />);
    expect(screen.getByLabelText(/^Base URL \(optional\)$/)).toHaveAttribute(
      'placeholder',
      'https://litellm.company.internal/v1',
    );
    expect(screen.getByText(/Leave empty to use Anthropic's API/)).toBeInTheDocument();
  });

  it('tests the saved key without sending one and shows success', async () => {
    testConnection.mockResolvedValue({
      ok: true,
      code: 'ok',
      message: 'Connected',
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      endpoint_host: 'api.deepseek.com',
      latency_ms: 420,
    });
    const { container } = render(<AuditJudgeSettings />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /test connection/i }));

    expect(testConnection).toHaveBeenCalledWith({
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      base_url: '',
      api_key: undefined,
    });
    expect(
      await screen.findByText('Connected to api.deepseek.com · deepseek-v4-pro · 420 ms'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('connection-test-status')).toHaveAttribute('aria-live', 'polite');
    // One live region: the Alert inside carries no role of its own.
    const region = screen.getByTestId('connection-test-status');
    expect(within(region).queryByRole('status')).toBeNull();
    expect(within(region).queryByRole('alert')).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('sends a typed key and shows the friendly error', async () => {
    testConnection.mockResolvedValue({
      ok: false,
      code: 'endpoint_not_api',
      message:
        "This address doesn't accept chat requests. platform.deepseek.com is DeepSeek's web console, not its API. Use https://api.deepseek.com, or leave Base URL empty to use DeepSeek's API.",
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      endpoint_host: 'platform.deepseek.com',
      latency_ms: 300,
    });
    const { container } = render(<AuditJudgeSettings />);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^API Key$/), 'sk-typed');
    await user.type(screen.getByLabelText(/^Base URL \(optional\)$/), 'https://platform.deepseek.com');
    await user.click(screen.getByRole('button', { name: /test connection/i }));

    const call = testConnection.mock.calls[0][0];
    expect(call.api_key).toBe('sk-typed');
    expect(call.base_url).toBe('https://platform.deepseek.com');
    expect(await screen.findByText("This address isn't a chat API")).toBeInTheDocument();
    expect(screen.getByText(/Use https:\/\/api\.deepseek\.com/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('asks for the key when the server answers key_required', async () => {
    testConnection.mockRejectedValue(
      new ApiError(
        400,
        JSON.stringify({ error: { code: 'key_required', message: 'Enter the key to test a different provider or endpoint.' } }),
      ),
    );
    render(<AuditJudgeSettings />);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^Base URL \(optional\)$/), 'https://llm.example.com/v1');
    await user.click(screen.getByRole('button', { name: /test connection/i }));

    expect(await screen.findByText('Enter the API key')).toBeInTheDocument();
    expect(screen.getByText(/Enter the API key to test a different provider or endpoint/)).toBeInTheDocument();
  });

  it('clears the result when a field changes', async () => {
    testConnection.mockResolvedValue({
      ok: true, code: 'ok', message: '', provider: 'deepseek', model: 'deepseek-v4-pro',
      endpoint_host: 'api.deepseek.com', latency_ms: 10,
    });
    render(<AuditJudgeSettings />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /test connection/i }));
    await screen.findByText(/Connected to api.deepseek.com/);
    await user.type(screen.getByLabelText(/^API Key$/), 'x');
    await waitFor(() => expect(screen.queryByText(/Connected to api.deepseek.com/)).toBeNull());
  });
});

describe('AuditJudgeSettings — save-time check', () => {
  it('shows a refused save on the Base URL field', async () => {
    const error = new ApiError(
      422,
      JSON.stringify({ error: { code: 'endpoint_not_api', message: "This address doesn't accept chat requests." } }),
    );
    const save = makeMutation({
      mutate: vi.fn((_body, opts) => opts.onError(error)),
    });
    judgeHooks.useSaveJudgeSettings.mockReturnValue(save);
    render(<AuditJudgeSettings />);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^API Key$/), 'sk-typed');
    await user.type(screen.getByLabelText(/^Base URL \(optional\)$/), 'https://platform.deepseek.com');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    const baseUrl = screen.getByLabelText(/^Base URL \(optional\)$/);
    expect(baseUrl).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText("This address doesn't accept chat requests.")).toBeInTheDocument();
  });

  it('shows warnings from a save as a non-blocking notice', async () => {
    const save = makeMutation({
      mutate: vi.fn((_body, opts) =>
        opts.onSuccess({ warnings: [{ code: 'timeout', message: "The provider didn't answer within 15 seconds." }] }),
      ),
    });
    judgeHooks.useSaveJudgeSettings.mockReturnValue(save);
    render(<AuditJudgeSettings />);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^API Key$/), 'sk-typed');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(await screen.findByText(/Saved, but the connection check didn't pass/)).toBeInTheDocument();
    expect(screen.getByText(/didn't answer within 15 seconds/)).toBeInTheDocument();
    expect(toastApi.addToast).toHaveBeenCalledWith('success', 'Settings saved');
  });
});
