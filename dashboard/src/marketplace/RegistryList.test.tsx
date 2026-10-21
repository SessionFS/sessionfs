import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { SkillRegistry } from '../api/client';
import { ApiError } from '../api/client';
import { RegistryList, pollBadge, relativeTime } from './RegistryList';

const { mutate, addToast } = vi.hoisted(() => ({ mutate: vi.fn(), addToast: vi.fn() }));

vi.mock('../hooks/useMarketplace', () => ({
  useRequestRegistryPoll: () => ({ mutate, isPending: false }),
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function registry(overrides: Partial<SkillRegistry> = {}): SkillRegistry {
  return {
    id: 'smr_1',
    owner_type: 'org',
    owner_id: 'org_1',
    org_id: 'org_1',
    alias: 'team',
    source_url: 'https://gitlab.example.com/acme/skills.git',
    refresh_commit: null,
    policy_state: 'approved',
    server_fetch_enabled: true,
    created_at: '2026-10-20T00:00:00Z',
    updated_at: '2026-10-20T00:00:00Z',
    poll_interval_seconds: 900,
    tag_pattern: 'v?\\d+\\.\\d+\\.\\d+',
    branch_only_mode: false,
    branch: null,
    last_poll_status: null,
    consecutive_failures: 0,
    backoff: false,
    ...overrides,
  };
}

describe('RegistryList', () => {
  it('maps poll state to one badge', () => {
    expect(pollBadge(registry({ server_fetch_enabled: false })).label).toBe('Metadata only');
    expect(pollBadge(registry()).label).toBe('Not polled yet');
    expect(pollBadge(registry({ last_poll_status: 'ok' })).variant).toBe('success');
    expect(pollBadge(registry({ last_poll_status: 'partial' })).variant).toBe('warning');
    expect(pollBadge(registry({ last_poll_status: 'error', backoff: true })).label).toBe('Failing · backing off');
  });

  it('formats relative times both ways', () => {
    const now = Date.parse('2026-10-20T12:00:00Z');
    expect(relativeTime('2026-10-20T11:50:00Z', now)).toBe('10 min ago');
    expect(relativeTime('2026-10-20T14:00:00Z', now)).toBe('in 2 h');
    expect(relativeTime(null, now)).toBeNull();
  });

  it('shows the failure summary and backoff, and lets an admin poll now', async () => {
    render(
      <RegistryList
        orgId="org_1"
        canAdmin
        registries={[
          registry({
            last_poll_status: 'error',
            last_poll_error_summary: 'fetch_blocked: gitlab.example.com resolves to a disallowed address',
            consecutive_failures: 3,
            backoff: true,
          }),
        ]}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('disallowed address');
    expect(screen.getByText(/3 consecutive failures/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Poll team now' }));
    expect(mutate).toHaveBeenCalledWith('smr_1', expect.any(Object));
    const options = mutate.mock.calls[0][1] as { onError: (error: unknown) => void };
    options.onError(new ApiError(429, JSON.stringify({ error: { code: 'poll_rate_limited', message: 'x' } })));
    expect(addToast).toHaveBeenCalledWith('error', 'A poll was requested less than a minute ago.');
  });

  it('hides Poll now for members and for metadata-only registries', () => {
    const { unmount } = render(<RegistryList orgId="org_1" canAdmin={false} registries={[registry()]} />);
    expect(screen.queryByRole('button', { name: /Poll .* now/ })).not.toBeInTheDocument();
    unmount();
    render(<RegistryList orgId="org_1" canAdmin registries={[registry({ server_fetch_enabled: false })]} />);
    expect(screen.queryByRole('button', { name: /Poll .* now/ })).not.toBeInTheDocument();
    expect(screen.getByText('Metadata only')).toBeInTheDocument();
  });
});
