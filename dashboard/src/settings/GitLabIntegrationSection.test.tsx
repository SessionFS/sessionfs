import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { GitLabIntegrationSection } from './GitLabIntegrationSection';

const mockAuth = vi.fn();
const mockGet = vi.fn();
const mockSave = vi.fn();
const mockDelete = vi.fn();
const addToast = vi.fn();

vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <GitLabIntegrationSection />
    </QueryClientProvider>,
  );
}

const NOT_CONNECTED = { instance_url: 'https://gitlab.com', has_token: false };
// The real GET also returns webhook_secret; it must never reach the screen.
const CONNECTED = {
  instance_url: 'https://gitlab.acme.example',
  has_token: true,
  webhook_secret: 'stored-webhook-secret-value',
};

describe('GitLabIntegrationSection', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockGet.mockReset();
    mockSave.mockReset();
    mockDelete.mockReset();
    addToast.mockReset();
    mockAuth.mockReturnValue({
      auth: {
        client: {
          getGitLabSettings: mockGet,
          saveGitLabSettings: mockSave,
          deleteGitLabSettings: mockDelete,
        },
      },
    });
  });

  it('shows the not-connected state with the default instance URL', async () => {
    mockGet.mockResolvedValue(NOT_CONNECTED);
    renderSection();
    expect(await screen.findByText('Not connected')).toBeInTheDocument();
    expect(screen.getByLabelText('Instance URL')).toHaveValue('https://gitlab.com');
    expect(screen.getByRole('button', { name: 'Connect GitLab' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull();
  });

  it('shows the connected instance and never renders a stored secret', async () => {
    mockGet.mockResolvedValue(CONNECTED);
    const { container } = renderSection();
    expect(await screen.findByRole('button', { name: 'Update GitLab' })).toBeInTheDocument();
    expect(screen.getByLabelText('Instance URL')).toHaveValue('https://gitlab.acme.example');
    expect(screen.getByLabelText('Access token')).toHaveValue('');
    expect(screen.getByLabelText('Webhook secret')).toHaveValue('');
    expect(container.innerHTML).not.toContain('stored-webhook-secret-value');
  });

  it('save success sends the form, clears the secret inputs and toasts', async () => {
    mockGet.mockResolvedValue(NOT_CONNECTED);
    mockSave.mockResolvedValue({ instance_url: 'https://gitlab.com', has_token: true });
    renderSection();
    await screen.findByText('Not connected');
    const token = screen.getByLabelText('Access token');
    const secret = screen.getByLabelText('Webhook secret');
    expect(token).toHaveAttribute('type', 'password');
    expect(secret).toHaveAttribute('type', 'password');
    fireEvent.change(token, { target: { value: 'glpat-abc' } });
    fireEvent.change(secret, { target: { value: 'hook-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitLab' }));
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith({
        instance_url: 'https://gitlab.com',
        access_token: 'glpat-abc',
        webhook_secret: 'hook-secret',
      }),
    );
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'GitLab settings saved'));
    expect(token).toHaveValue('');
    expect(secret).toHaveValue('');
  });

  it('shows the server validation error and does not toast', async () => {
    mockGet.mockResolvedValue(NOT_CONNECTED);
    mockSave.mockRejectedValue(new ApiError(422, JSON.stringify({ detail: 'instance_url must be https' })));
    renderSection();
    await screen.findByText('Not connected');
    fireEvent.change(screen.getByLabelText('Access token'), { target: { value: 'glpat-abc' } });
    fireEvent.change(screen.getByLabelText('Webhook secret'), { target: { value: 's' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitLab' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('instance_url must be https');
    expect(addToast).not.toHaveBeenCalled();
  });

  it('disconnect requires confirmation', async () => {
    mockGet.mockResolvedValue(CONNECTED);
    mockDelete.mockResolvedValue(undefined);
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));
    expect(mockDelete).not.toHaveBeenCalled();
    let dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Disconnect GitLab?');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(mockDelete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'GitLab disconnected'));
  });
});
